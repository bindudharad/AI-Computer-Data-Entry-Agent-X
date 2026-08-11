"""Action executor.

Executes planned actions through the control engine, verifies every value-
producing action, and coordinates retries with the recovery planner. The
executor never continues blindly: a failed verification triggers corrective
actions (retry / refocus / re-analyse) up to the configured budget.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from atlas.act.controls import ControlInterface
from atlas.act.keyboard import HumanKeyboard
from atlas.act.models import VERIFYABLE_ACTIONS, Action, ActionResult, ActionType
from atlas.act.mouse import HumanMouse
from atlas.act.sandbox import ExecutionSandbox
from atlas.act.verification import (
    VerificationEngine,
    VerificationResult,
    VerificationStatus,
)
from atlas.act.verify import CompositeVerifier
from atlas.core.logging import action_logger, logger, verification_logger
from atlas.core.metrics import Timer
from atlas.reason.recovery import RecoveryDecision, RecoveryPlanner
from atlas.vision.models import BBox, SceneDescription

if TYPE_CHECKING:
    from atlas.reason.planner import FillPlan

SceneProvider = Callable[[], SceneDescription | None]


class ActionExecutor:
    """Executes actions with verification and recovery."""

    def __init__(
        self,
        mouse: HumanMouse,
        keyboard: HumanKeyboard,
        controls: ControlInterface,
        verifier: CompositeVerifier | VerificationEngine,
        recovery: RecoveryPlanner,
        verify_after_action: bool = True,
        max_retries: int = 3,
        retry_delay: float = 0.8,
        scene_provider: SceneProvider | None = None,
        reobserve: Callable[[], SceneDescription | None] | None = None,
        sandbox: ExecutionSandbox | None = None,
        max_scroll_attempts: int = 6,
        scroll_amount: int = 3,
        debug_dir: str | Path | None = None,
        read_recovery_attempts: int = 2,
        noop_detect: bool = False,
    ) -> None:
        self._mouse = mouse
        self._keyboard = keyboard
        self._controls = controls
        self._verifier = verifier if isinstance(verifier, VerificationEngine) else VerificationEngine(verifier)
        self._recovery = recovery
        self._verify_after_action = verify_after_action
        self._max_retries = max_retries
        self._retry_delay = retry_delay
        self._scene_provider = scene_provider
        self._reobserve = reobserve
        self._sandbox = sandbox
        self._max_scroll_attempts = max_scroll_attempts
        self._scroll_amount = scroll_amount
        self._scroll_allowed: Callable[[], bool] | None = None
        #: When set (the reveal pass runs DOWN-only), scroll-into-view may never
        #: scroll UP - a failed scroll is retried forward, never reversed.
        self._scroll_direction_override: str | None = None
        #: Refreshes an action's bbox from live geometry (e.g. the field-map
        #: queue by stable id) immediately before verification, so a read never
        #: uses a bbox made stale by a scroll or window resize since the write.
        self._bbox_refresher: Callable[[str | None], BBox | None] | None = None
        self._debug_dir = Path(debug_dir) if debug_dir else None
        #: Read-only re-reads allowed for an UNKNOWN verification before the
        #: field is accepted as written-but-unconfirmed (never re-runs the
        #: action itself).
        self._read_recovery_attempts = max(1, int(read_recovery_attempts))
        #: When enabled, a verifiable action is pre-checked BEFORE it runs: if
        #: the field already holds the target value (e.g. a Sub Caste /
        #: Nakshatra field that was not actually reset), the write is skipped
        #: entirely and reported ALREADY_CORRECT.
        self._noop_detect = bool(noop_detect)
        #: Per-field stage timings (action/verify/recovery) aggregated across
        #: a run, flushed by the workflow to ``debug/performance/run_metrics.json``.
        self._field_metrics: dict[str, dict] = {}
        #: Structured verification events (geometry + outcome) flushed by the
        #: workflow loop to ``verification_debug.json`` for post-mortem analysis.
        self._verification_events: list[dict] = []

    def set_reobserve(self, reobserve: Callable[[], SceneDescription | None]) -> None:
        self._reobserve = reobserve

    def set_bbox_refresher(self, refresher: Callable[[str | None], BBox | None]) -> None:
        """Wire live geometry refresh used just before every verification read."""
        self._bbox_refresher = refresher

    def verification_events(self) -> list[dict]:
        """Structured verification events (expected/observed/geometry/outcome)."""
        return list(self._verification_events)

    def field_metrics(self) -> dict[str, dict]:
        """Per-field stage timings for the run (see ``_record_stage``)."""
        return {
            fid: dict(entry)
            for fid, entry in sorted(self._field_metrics.items())
        }

    def set_scroll_direction(self, direction: str | None) -> None:
        """Lock the direction ``_scroll_into_view`` may use (``"down"`` etc.).

        ``None`` lifts the override. Used by the reveal pass so the scan is
        DOWN-only until the Upload Details section is reached - it never
        reverses a scroll that failed.
        """
        self._scroll_direction_override = direction

    def set_scroll_allowed(self, gate: Callable[[], bool] | None) -> None:
        """Control whether ``_scroll_into_view`` may scroll the page.

        The workflow uses this as the scroll lock: while a viewport is being
        observed or filled, the gate returns False so no scroll-into-view can
        fire before the viewport is complete. When the gate is None, scrolling
        is always allowed (default, backwards compatible).
        """
        self._scroll_allowed = gate

    def _scroll_allowed_now(self) -> bool:
        if self._scroll_allowed is None:
            return True
        try:
            return bool(self._scroll_allowed())
        except Exception:
            return True

    # -- public API ----------------------------------------------------------

    def execute_plan(self, plan: FillPlan) -> list[ActionResult]:
        results: list[ActionResult] = []
        for action in plan.actions:
            result = self.execute(action)
            results.append(result)
            if result.action.type in {ActionType.STOP} and not result.success:
                break
        return results

    def execute(self, action: Action) -> ActionResult:
        with Timer() as timer:
            result = self._execute_with_recovery(action)
        result.duration_ms = timer.elapsed * 1000.0
        from atlas.core.events import EventType, get_event_bus

        failed = not (result.ok or result.verified)
        payload = result.to_dict()
        get_event_bus().publish(
            EventType.ACTION_FAILED if failed else EventType.ACTION_COMPLETED,
            payload,
        )
        action_logger.info(
            "action {}{}: {} ({}) in {:.0f}ms | retries={}",
            action.type.value,
            " FAILED" if failed else "",
            action.reason,
            result.message or "ok",
            timer.elapsed * 1000.0,
            result.retries,
        )
        if failed:
            logger.warning(
                "action failed: {} ({}): {}",
                action.type.value,
                action.reason,
                result.message,
            )
        return result

    # -- internals -----------------------------------------------------------

    def _record_stage(self, action: Action, stage: str, seconds: float) -> None:
        """Accumulate per-field stage timing for ``run_metrics.json``."""
        key = action.field_id or action.reason or action.type.value
        entry = self._field_metrics.setdefault(key, {
            "field_id": action.field_id,
            "label": action.reason,
            "stages": {},
        })
        stages = entry["stages"]
        stages[stage] = stages.get(stage, 0.0) + float(seconds)
        entry["total_s"] = sum(stages.values())

    def _execute_with_recovery(self, action: Action) -> ActionResult:
        # Uploads must never be re-clicked: a retry could double-submit the form.
        # Execute once, then stop the record if it fails so the loop can move on.
        if action.type == ActionType.SUBMIT:
            if self._sandbox is not None and self._sandbox.is_paused:
                self._sandbox.wait_until_resumed()
            if not self._assert_sandbox(action):
                return ActionResult(action=action, success=False, message="sandbox blocked submit")
            self._scroll_into_view(action)
            return self._do(action, 0)

        max_retries = action.max_retries if action.max_retries is not None else self._max_retries
        if self._noop_detect and action.type in VERIFYABLE_ACTIONS and action.value is not None:
            noop = self._check_already_correct(action)
            if noop is not None:
                return noop
        for attempt in range(max_retries + 1):
            # Check sandbox before each attempt.
            if self._sandbox is not None and self._sandbox.is_paused:
                logger.warning("sandbox paused - waiting for resume")
                self._sandbox.wait_until_resumed()
            if not self._assert_sandbox(action):
                result = ActionResult(action=action, success=False, message="sandbox blocked action")
                return result
            self._scroll_into_view(action, attempt)
            with Timer() as action_timer:
                result = self._do(action, attempt)
            self._record_stage(action, "action", action_timer.elapsed)
            if action.type is ActionType.VERIFY:
                # A standalone VERIFY already runs verification inside its
                # dispatch and carries its own honest status: return it as-is
                # so an UNKNOWN re-read is never forced to verified=True.
                return result
            if action.type not in VERIFYABLE_ACTIONS or not self._verify_after_action:
                result.verified = True
                return result
            with Timer() as verify_timer:
                vresult = self._verify(action)
            self._record_stage(action, "verify", verify_timer.elapsed)
            result.verified = vresult.is_match or vresult.status is VerificationStatus.NOT_APPLICABLE
            result.verification_evidence = vresult.evidence
            result.verification_status = vresult.status.value
            self._publish_verification(action, vresult, attempt, geometry=self._current_geometry())
            if vresult.is_match or vresult.status is VerificationStatus.NOT_APPLICABLE:
                if action.field_id:
                    self._recovery.on_success(action.field_id)
                return result

            if vresult.is_unknown:
                # The action executed but the read-back is unreadable (empty
                # UIA read, whole-window clipboard grab, OCR blank, ...).
                # This is NOT a mismatch: re-running the action would just
                # re-type into a field whose value may already be correct.
                # Accept the field as written with an honest UNKNOWN status
                # (ACTION_SUCCESS_VERIFICATION_UNKNOWN) and move on - BUT
                # never a verified pass: ``verified`` stays False so the
                # UNKNOWN write is surfaced and tracked, not silently counted
                # as a confirmed success.
                result.message = f"verification UNKNOWN after action - accepted as written but NOT verified ({vresult.method or 'composite'})"
                if action.field_id:
                    self._recovery.on_success(action.field_id)
                return result

            with Timer() as recovery_timer:
                decision = self._recovery.decide(
                    action,
                    result,
                    self._scene_provider() if self._scene_provider else None,
                    verification_status=result.verification_status,
                )
                if decision.skip_field or decision.stop_record:
                    result.success = False
                    result.message = decision.reason
                    return result
                self._apply_correction(decision, action)
            self._record_stage(action, "recovery", recovery_timer.elapsed)
            time.sleep(self._retry_delay)

        result.success = False
        result.message = f"action exhausted {max_retries + 1} attempts"
        return result

    def _publish_verification(self, action: Action, vresult: VerificationResult, attempt: int, geometry: dict | None = None) -> None:
        from atlas.core.events import EventType, get_event_bus

        status = vresult.status.value
        payload = {
            "field_id": action.field_id,
            "label": action.reason,
            "expected": action.expected or action.value,
            "observed": vresult.observed,
            "ok": vresult.is_match,
            "status": status,
            "method": vresult.method,
            "attempt": attempt,
            "evidence": vresult.evidence,
            "geometry": geometry or {},
        }
        get_event_bus().publish(EventType.VERIFICATION, payload)
        self._verification_events.append(payload)
        verification_logger.info(
            "verify {} [{}] expected={!r} observed={!r} -> {} (attempt {})",
            action.field_id or action.reason,
            action.type.value,
            action.expected or action.value,
            vresult.observed,
            status,
            attempt,
        )
        if not vresult.is_match:
            geom = payload["geometry"]
            logger.debug(
                "verify {} {} expected={!r} observed={!r} geometry={}",
                action.field_id or action.reason,
                status,
                payload["expected"],
                vresult.observed,
                geom,
            )

    def _apply_correction(self, decision: RecoveryDecision, action: Action) -> None:
        logger.info("recovery: {}", decision.reason)
        from atlas.core.events import EventType, get_event_bus

        get_event_bus().publish(EventType.RECOVERY, decision.to_dict())
        if action.type == ActionType.SELECT and decision.action in {ActionType.WAIT, ActionType.CLICK}:
            # A dropdown that doesn't close after Enter - e.g. a cascading
            # list (like a Sub Caste list driven by a Caste choice) that was
            # still repopulating at the moment the selection fired - leaves
            # an open popup floating over the screen. That stray popup then
            # intercepts the click/verify for THIS retry, and often for
            # several unrelated fields after it too, since the popup can
            # overlap whatever sits below it. That is the "a run of fields
            # all fail for no clear reason right after a dropdown" pattern.
            # Escape is the standard, safe way to dismiss any open combobox
            # popup and is a no-op if nothing is open, so it is safe to send
            # unconditionally before every dropdown retry.
            try:
                self._keyboard.press("escape")
            except Exception:
                pass
        if decision.action == ActionType.CLICK and action.bbox is not None:
            self._mouse.click(*action.bbox.center)
        elif decision.action == ActionType.SCROLL and self._scroll_allowed_now():
            # A recovery scroll must never fire while the viewport is still
            # being observed/filled (NO SCROLL RULE). If the viewport is not
            # complete the correction is a no-op; the workflow keeps filling.
            self._controls.scroll("down", 3)
        elif decision.action == ActionType.WAIT:
            time.sleep(action.wait_seconds)
        elif decision.action == ActionType.ANALYZE and self._reobserve is not None:
            scene = self._reobserve()
            if scene is not None and action.field_id:
                element = scene.element(action.field_id)
                if element is not None and element.bbox is not None:
                    action.bbox = element.bbox.shifted(*scene.screen_offset)

    def _assert_sandbox(self, action: Action) -> bool:
        """Validate action against sandbox rules. Returns False if blocked."""
        if self._sandbox is None:
            return True
        # Keyboard actions require focus check.
        if action.type in {ActionType.TYPE, ActionType.CLEAR, ActionType.PASTE, ActionType.TAB,
                           ActionType.PRESS_ENTER, ActionType.PRESS_ESCAPE, ActionType.SUBMIT}:
            ok, reason = self._sandbox.validate_keyboard()
            if not ok:
                logger.warning("sandbox blocked keyboard: {}", reason)
                return False
        # Mouse actions require click validation.
        if action.type in {ActionType.CLICK, ActionType.DOUBLE_CLICK, ActionType.RIGHT_CLICK, ActionType.HOVER} and action.bbox is not None:
            x, y = action.bbox.center
            ok, reason = self._sandbox.validate_click(x, y)
            if not ok:
                logger.warning("sandbox blocked click: {}", reason)
                return False
        return True

    def _scroll_into_view(self, action: Action, attempt: int = 0) -> None:
        """Bring an off-viewport field into view before acting on it.

        When the action targets a bbox outside the target client rect (e.g. a
        field below the fold), scroll toward it and re-observe to refresh the
        bbox. The scroll strategy escalates when one method makes no progress:

        - mouse wheel first (most natural),
        - keyboard PageUp/PageDown when the wheel scrolls a parent pane but
          not the nested region holding the field,
        - scroll-bar jump (End/Home) as a last resort.

        Bounded and never fatal: if it cannot be brought into view the action
        is left as-is so sandbox validation still blocks it safely.

        The visibility check itself is UNCONDITIONAL - it runs even while the
        round-level reveal pass holds the scroll lock (`_lock_scrolling`).
        Only the lock's job (deciding whether the executor may run its own
        full, multi-strategy exploratory scroll) is gated; whether a field is
        actually on-screen right now must always be checked, and if it is
        not, at least one corrective nudge is always allowed. A batch of
        "visible" actions is planned once from a single scene snapshot, but
        that snapshot can go stale mid-batch (an earlier action in the same
        batch reflows the page, a dropdown opens and shifts layout, a
        server-driven update lands) - refusing to correct for that here means
        the click for that field silently lands nowhere, focus stays on the
        PREVIOUS field, and clear_field()'s Ctrl+A then wipes that field's
        content instead of the intended one. That silent failure mode - never
        an exception, just a field quietly clicked and cleared on the wrong
        target - is what made it look like the loop "gets stuck" without ever
        scrolling. Always confirming visibility here, exactly as done before
        every single field fill in the reference implementation this was
        ported from, closes that gap without touching how the round-level
        pass owns and paces its own bulk scrolling.

        ``attempt`` closes a second, subtler gap: the "is the bbox still
        inside the client rect" check above only catches a field that left
        the viewport entirely. It does NOT catch a field that merely SHIFTED
        by a modest amount - e.g. a cascading dropdown (a Sub Caste list
        driven by a Caste choice) reflowing the layout below it - because the
        stale bbox can still land inside the client rect while pointing at
        the wrong spot entirely (blank space, a neighbouring control, or
        nothing). That produces a very specific, previously mysterious
        failure signature: the click "succeeds" (no exception), but the
        OCR/clipboard verification reads back nothing for that field AND for
        every field after it in the same batch, since they all came from the
        one stale pre-batch snapshot and are now offset by the same reflow.
        On the FIRST attempt this cost is not worth paying (the common case
        is a batch whose snapshot is still accurate); once an attempt has
        already failed verification, though, the field's true current bbox
        is looked up fresh, unconditionally, before retrying - cheap relative
        to the four-attempt retry ladder it replaces, and it self-heals the
        rest of the batch too since each field re-checks itself the same way.
        """
        if action.bbox is None:
            return
        if self._sandbox is None:
            return
        if attempt > 0 and action.field_id is not None and self._reobserve is not None:
            try:
                scene = self._reobserve()
            except Exception as exc:
                scene = None
                logger.debug("bbox refresh re-observe failed: {}", exc)
            if scene is not None:
                element = scene.element(action.field_id)
                if element is not None and element.bbox is not None:
                    action.bbox = element.bbox.shifted(*scene.screen_offset)
        target = self._sandbox.validate_target()
        if target is None or not target.client_rect:
            return
        left, top, right, bottom = target.client_rect
        cx, cy = action.bbox.center
        if left <= cx <= right and top <= cy <= bottom:
            return  # already visible - the common case; nothing to correct
        # Confirmed off-screen. While the round-level lock is held, only one
        # corrective nudge is allowed (never the full exploratory ladder) so
        # this cannot race or fight the reveal pass's own synchronized,
        # multi-panel scrolling - it only rescues the one field that would
        # otherwise be silently missed.
        allowed_attempts = self._max_scroll_attempts if self._scroll_allowed_now() else 1
        strategies: list[str] = ["wheel", "keys", "scrollbar"]
        previous_center: tuple[int, int] | None = None
        for _ in range(allowed_attempts):
            cx, cy = action.bbox.center
            if left <= cx <= right and top <= cy <= bottom:
                return
            # Escalate when the last attempt moved nothing.
            if previous_center == (cx, cy) and strategies:
                strategies.pop(0)
                if not strategies:
                    return
            previous_center = (cx, cy)

            if cy < top:
                direction = "up"
            elif cy > bottom:
                direction = "down"
            elif cx < left:
                direction = "up"
            else:
                direction = "down"
            # NEVER REVERSE SCROLL while the reveal pass owns the scroll: a
            # failed scroll is retried forward with the next method / distance.
            if self._scroll_direction_override:
                direction = self._scroll_direction_override

            strategy = strategies[0]
            if strategy == "keys":
                self._controls.scroll_by_keys(direction, self._scroll_amount)
            elif strategy == "scrollbar":
                self._controls.scroll_bar(direction, self._scroll_amount)
            else:
                self._controls.scroll(direction, self._scroll_amount)

            time.sleep(self._retry_delay)
            if self._reobserve is None:
                continue
            scene = self._reobserve()
            if scene is None or action.field_id is None:
                continue
            element = scene.element(action.field_id)
            if element is not None and element.bbox is not None:
                action.bbox = element.bbox.shifted(*scene.screen_offset)

    def _refresh_geometry(self, action: Action) -> None:
        """Refresh the action's bbox from live geometry before the WRITE.

        Verification already refreshes (see ``_verify``); the write must too,
        otherwise a scroll or reflow since the plan was built makes the click /
        type / dropdown-open land on a stale spot - the classic "combo select
        leaves the placeholder" failure for below-the-fold fields like Rashi.
        No-op outside the field-map path (``refresh_action_bbox`` returns None),
        where the viewport path re-observes instead.
        """
        if action.bbox is None or self._bbox_refresher is None or not action.field_id:
            return
        try:
            fresh = self._bbox_refresher(action.field_id)
            if fresh is not None and fresh.width > 0 and fresh.height > 0:
                action.bbox = fresh
        except Exception:
            pass

    def _ensure_target_foreground(self) -> None:
        """Raise the attached window to foreground before a write.

        Combobox interactions (and any focus-dependent write) silently no-op
        when the target is not the foreground window: the open-click lands
        without opening the dropdown, and the typed keys go to whichever window
        actually owns focus. Reuses the sandbox's activation logic; cheap
        no-op when focus is already on the target.
        """
        if self._sandbox is None:
            return
        try:
            import win32gui

            target = self._sandbox.validate_target()
            if target is None or not getattr(target, "handle", None):
                return
            if win32gui.GetForegroundWindow() == target.handle:
                return
            ExecutionSandbox._refocus_target(target)
        except Exception as exc:
            logger.debug("foreground raise failed: {}", exc)

    def _do(self, action: Action, attempt: int) -> ActionResult:
        try:
            return self._dispatch(action, attempt)
        except Exception as exc:
            logger.debug("action dispatch error: {}", exc)
            return ActionResult(action=action, success=False, message=str(exc), retries=attempt)

    def _dispatch(self, action: Action, attempt: int) -> ActionResult:
        self._refresh_geometry(action)
        self._ensure_target_foreground()
        bbox = action.bbox
        value = action.value

        if action.type == ActionType.MOVE_MOUSE and bbox:
            self._mouse.move_to(*bbox.center)
        elif action.type == ActionType.CLICK:
            if bbox is not None or action.field_id:
                outcome = self._controls.click_field(bbox, action.field_id)
                if not outcome.ok:
                    return ActionResult(action=action, success=False, message=outcome.evidence)
            else:
                self._controls.press_enter()  # default: activate focused control
        elif action.type == ActionType.DOUBLE_CLICK and bbox:
            self._mouse.double_click(*bbox.center)
        elif action.type == ActionType.RIGHT_CLICK and bbox:
            self._mouse.right_click(*bbox.center)
        elif action.type == ActionType.HOVER and bbox:
            self._mouse.hover(*bbox.center)
        elif action.type == ActionType.SCROLL:
            self._controls.scroll("down", action.scroll_amount)
        elif action.type == ActionType.TYPE and value is not None:
            self._controls.type_value(bbox, value, action.field_id)
        elif action.type == ActionType.CLEAR:
            self._controls.clear(bbox, action.field_id)
        elif action.type == ActionType.SELECT and value is not None:
<<<<<<< HEAD
            outcome = self._controls.select_option(bbox, value, action.options, action.field_id)
            if not outcome.ok:
                return ActionResult(action=action, success=False, message=outcome.evidence)
=======
            self._controls.select_option(bbox, value, action.options, action.field_id)
>>>>>>> 506caa78300fd5640f3fd0dcb51ac6f142dcd8ca
        elif action.type == ActionType.TOGGLE and value is not None:
            self._controls.toggle(bbox, value, action.field_id)
        elif action.type == ActionType.CHOOSE_DATE and value is not None:
            self._controls.choose_date(bbox, value, None, action.field_id)
        elif action.type == ActionType.TAB:
            self._controls.press_tab()
        elif action.type == ActionType.PRESS_ENTER:
            self._controls.press_enter()
        elif action.type == ActionType.PRESS_ESCAPE:
            self._controls.press_escape()
        elif action.type == ActionType.PASTE:
            self._controls.paste(value or "", action.field_id)
        elif action.type == ActionType.UPLOAD_FILE:
            if value is None:
                return ActionResult(action=action, success=False, message="no file path for upload")
            outcome = self._controls.upload_file(bbox, value, action.field_id)
            if not outcome.ok:
                return ActionResult(action=action, success=False, message=outcome.evidence)
        elif action.type == ActionType.WAIT:
            time.sleep(action.wait_seconds)
        elif action.type == ActionType.SUBMIT:
            if bbox is not None or action.field_id:
                outcome = self._controls.click_field(bbox, action.field_id)
                if not outcome.ok:
                    return ActionResult(action=action, success=False, message=outcome.evidence)
            else:
                self._controls.press_enter()
        elif action.type in {ActionType.CAPTURE, ActionType.ANALYZE}:
            return ActionResult(action=action, success=True, verified=True, message="handled by loop")
        elif action.type == ActionType.VERIFY:
            if action.value is not None:
                vresult = self._verify(action)
                # A standalone VERIFY must never trigger a re-fill: MATCH and
                # NOT_APPLICABLE accept, and UNKNOWN accepts with an honest
                # status (the read-recovery ladder already ran); only a genuine
                # MISMATCH fails the verify action.
                failed = vresult.status is VerificationStatus.MISMATCH
                return ActionResult(
                    action=action, success=not failed, verified=vresult.is_match,
                    message=vresult.evidence, verification_evidence=vresult.evidence,
                    verification_status=vresult.status.value,
                )
            return ActionResult(action=action, success=True, verified=True, message="nothing to verify")
        elif action.type == ActionType.STOP:
            return ActionResult(action=action, success=False, verified=False, message="stop requested")
        else:
            return ActionResult(action=action, success=False, message=f"unsupported action {action.type.value}")

        return ActionResult(action=action, success=True, retries=attempt)

    def _check_already_correct(self, action: Action) -> ActionResult | None:
        """Return an ALREADY_CORRECT result when the field already holds the
        target value (no-op detection), else None.

        Only called for verifiable value actions when ``noop_detect`` is on.
        A pre-write MATCH means the write can be skipped entirely - this is
        the Sub Caste / Nakshatra "reset" case where the value never actually
        changed. Reported as ``ACTION_SUCCESS_VERIFICATION_ALREADY_CORRECT``
        (verified, so it never counts as UNKNOWN and never re-fills).
        """
        with Timer() as pre_timer:
            vresult = self._verify(action)
        self._record_stage(action, "noop", pre_timer.elapsed)
        if not vresult.is_match:
            return None
        result = ActionResult(
            action=action,
            success=True,
            verified=True,
            verification_status="ALREADY_CORRECT",
            verification_evidence=vresult.evidence,
            message=f"field already holds the value - write skipped (no-op, {vresult.method or 'composite'})",
        )
        if action.field_id:
            self._recovery.on_success(action.field_id)
        self._publish_verification(action, vresult, 0, geometry=self._current_geometry())
        return result

    def _verify(self, action: Action) -> VerificationResult:
        if action.value is None:
            return VerificationResult(
                status=VerificationStatus.NOT_APPLICABLE,
                field_id=action.field_id,
                expected=action.value,
                method="none",
                evidence="nothing to verify",
            )
        # Refresh geometry before reading: the bbox used to WRITE can go stale
        # by the time we READ (scroll settle, dropdown reflow, window resize).
        # The loop supplies the live field-map position by stable id.
        if action.bbox is not None and self._bbox_refresher is not None and action.field_id:
            try:
                fresh = self._bbox_refresher(action.field_id)
                if fresh is not None and fresh.width > 0 and fresh.height > 0:
                    action.bbox = fresh
            except Exception:
                pass
        return self._verifier.verify_with_read_recovery(
            action.bbox,
            action.value,
            action.field_id,
            max_attempts=self._read_recovery_attempts,
            refocus=self._read_refocus_for(action),
        )

    def _read_refocus_for(self, action: Action) -> Callable[[BBox], None] | None:
        """Return a read-only refocus callback for an action, or None.

        Refocus only applies to text-style actions (TYPE/CLEAR/PASTE). For a
        SELECT/TOGGLE/CHOOSE_DATE the "refocus" step would click the control
        again and could OPEN the dropdown - which would change the very value
        being read back - so no refocus is used there.
        """
        if action.type not in {ActionType.TYPE, ActionType.CLEAR, ActionType.PASTE}:
            return None

        def _refocus(bbox: BBox) -> None:
            try:
                self._mouse.click(*bbox.center)
            except Exception as exc:
                logger.debug("read-recovery refocus failed: {}", exc)

        return _refocus

    def _current_geometry(self) -> dict:
        """Current target geometry, recorded with every verification event.

        A stale bbox (window resized or scrolled between the write and the
        read) is the classic cause of "vision read empty"; the recorded client
        rect proves or rules that out in post-mortem analysis.
        """
        geometry: dict = {}
        if self._sandbox is None:
            return geometry
        try:
            target = self._sandbox.validate_target()
            if target is not None and target.client_rect:
                left, top, right, bottom = target.client_rect
                if right > left and bottom > top:
                    geometry["client_rect"] = [left, top, right, bottom]
                    geometry["client_size"] = [right - left, bottom - top]
        except Exception:
            pass
        return geometry


__all__ = ["ActionExecutor", "SceneProvider"]
