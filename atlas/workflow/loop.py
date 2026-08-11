"""The agent workflow loop.

Orchestrates the Observe -> Understand -> Reason -> Plan -> Execute -> Verify
loop for a stream of source records, target-agnostic (desktop window or web
page). It drives every stage explicitly, emits events and state transitions,
and refuses to continue past a record whose actions failed verification.

    while records remain:
        observe    -> target.observe()                       (VLM scene)
        understand -> SourceReader  -> SourceRecord
                     discover_fields -> editable fields
        reason     -> SemanticMapper -> MappingResult
        plan       -> ActionPlanner  -> FillPlan
        execute    -> ActionExecutor  -> verified results
        verify     -> every value-producing action verified (executor)
        next       -> poll until the source record changes, or timeout
"""

from __future__ import annotations

import json
import random
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from atlas.act.executor import ActionExecutor
from atlas.act.models import Action, ActionResult, ActionType
from atlas.core.events import EventType, get_event_bus
<<<<<<< HEAD
from atlas.core.logging import log_screenshot, logger, watchdog_logger
=======
from atlas.core.logging import log_screenshot, logger
>>>>>>> 506caa78300fd5640f3fd0dcb51ac6f142dcd8ca
from atlas.core.metrics import Timer
from atlas.core.record_builder import RecordBuilder, RecordBuildResult
from atlas.core.states import AgentState, StateMachine
from atlas.mapping.mapper import MappingResult, SemanticMapper
from atlas.mapping.uia_map import UiaFieldMap, pair_source_pairs
from atlas.observe.screen_state import build_screen_state
from atlas.observe.uia import ScrollContainer
from atlas.reason.planner import ActionPlanner, FillPlan
from atlas.reason.sections import find_upload_sections
from atlas.target.base import TargetAdapter
from atlas.understanding.fields import discover_fields
from atlas.understanding.source import SourceReader, SourceRecord
from atlas.vision.models import BBox, ElementType, OcrText, SceneDescription, ScreenElement
from atlas.vision.scene import SceneAnalysis
from atlas.workflow.field_engine import (
    DEFAULT_FIELD_RETRIES,
    DEFAULT_FIELD_TIMEOUT,
    DEFAULT_SCROLL_ATTEMPTS,
    DateGroupTarget,
<<<<<<< HEAD
    FieldStatus,
=======
>>>>>>> 506caa78300fd5640f3fd0dcb51ac6f142dcd8ca
    PendingFieldQueue,
    PerfTracker,
    ProgressGuard,
    ScrollCapabilityCache,
    ScrollProgress,
    TargetNavigator,
    build_field_actions,
    build_field_queue,
<<<<<<< HEAD
    classify_fill_status,
    field_coverage_summary,
=======
>>>>>>> 506caa78300fd5640f3fd0dcb51ac6f142dcd8ca
    make_scroll_fn,
)
from atlas.workflow.scroll import PANEL_LEFT, PANEL_RIGHT, DualPanelScroll
from atlas.workflow.scroller import ScrollSession, pick_left_right_containers
from atlas.workflow.viewport import ViewportModel

ACTION_STATE = {
    ActionType.TYPE: AgentState.TYPING,
    ActionType.PASTE: AgentState.TYPING,
    ActionType.CLEAR: AgentState.TYPING,
    ActionType.SELECT: AgentState.TYPING,
    ActionType.TOGGLE: AgentState.TYPING,
    ActionType.CHOOSE_DATE: AgentState.TYPING,
    ActionType.TAB: AgentState.TYPING,
    ActionType.PRESS_ENTER: AgentState.TYPING,
    ActionType.PRESS_ESCAPE: AgentState.TYPING,
    ActionType.CLICK: AgentState.CLICKING,
    ActionType.DOUBLE_CLICK: AgentState.CLICKING,
    ActionType.RIGHT_CLICK: AgentState.CLICKING,
    ActionType.HOVER: AgentState.CLICKING,
    ActionType.MOVE_MOUSE: AgentState.CLICKING,
    ActionType.SUBMIT: AgentState.UPLOADING,
    ActionType.SCROLL: AgentState.SCROLLING,
    ActionType.WAIT: AgentState.WAITING,
    ActionType.VERIFY: AgentState.VERIFYING,
    ActionType.CAPTURE: AgentState.ANALYZING,
    ActionType.ANALYZE: AgentState.ANALYZING,
    ActionType.STOP: AgentState.STOPPED,
}

#: Human-readable operation shown on the status panel while each action runs,
#: so the operator always sees the CURRENT operation (never a stale "READING"
#: while the agent is writing / selecting / scrolling).
ACTION_DETAIL = {
    ActionType.TYPE: "WRITING",
    ActionType.PASTE: "WRITING",
    ActionType.CLEAR: "WRITING",
    ActionType.TOGGLE: "WRITING",
    ActionType.CHOOSE_DATE: "WRITING",
    ActionType.TAB: "WRITING",
    ActionType.PRESS_ENTER: "WRITING",
    ActionType.PRESS_ESCAPE: "WRITING",
    ActionType.SELECT: "SELECTING",
    ActionType.CLICK: "CLICKING",
    ActionType.DOUBLE_CLICK: "CLICKING",
    ActionType.RIGHT_CLICK: "CLICKING",
    ActionType.HOVER: "CLICKING",
    ActionType.MOVE_MOUSE: "CLICKING",
    ActionType.SUBMIT: "UPLOADING",
    ActionType.SCROLL: "SCROLLING",
    ActionType.VERIFY: "VERIFYING",
}


@dataclass
class RecordResult:
    """Outcome of processing one source record."""

    index: int
    record: SourceRecord
    mapping: MappingResult
    actions: list[ActionResult] = field(default_factory=list)
    skipped_fields: list[str] = field(default_factory=list)
    success: bool = False
    incomplete_fields: list[str] = field(default_factory=list)
    #: Fields accepted as written with an UNKNOWN verification - tracked and
    #: surfaced (never counted as a verified pass).
    unverified_fields: list[str] = field(default_factory=list)
    duration_ms: float = 0.0
    message: str = ""

    def to_dict(self) -> dict:
        return {
            "index": self.index,
            "record": self.record.to_dict(),
            "mapping": self.mapping.to_dict(),
            "actions": [a.to_dict() for a in self.actions],
            "skipped_fields": list(self.skipped_fields),
            "success": self.success,
            "incomplete_fields": list(self.incomplete_fields),
            "unverified_fields": list(self.unverified_fields),
            "duration_ms": self.duration_ms,
            "message": self.message,
        }


@dataclass
class WorkflowSummary:
    """Aggregate of a whole workflow run."""

    records: list[RecordResult] = field(default_factory=list)
    started: float = field(default_factory=time.time)
    finished: float = 0.0
    stopped_reason: str = ""

    @property
    def completed(self) -> int:
        return sum(1 for r in self.records if r.success)

    @property
    def failed(self) -> int:
        return sum(1 for r in self.records if not r.success)

    @property
    def unverified(self) -> int:
        """Records containing at least one field written with UNKNOWN verification."""
        return sum(1 for r in self.records if r.unverified_fields)

    @property
    def unverified_fields(self) -> int:
        """Total UNKNOWN-written fields across the run."""
        return sum(len(r.unverified_fields) for r in self.records)

    @property
    def blocked_fields(self) -> list[tuple[str, str, str]]:
        """All value-type/ambiguity-rejected pairings across the run."""
        out: list[tuple[str, str, str]] = []
        for r in self.records:
            out.extend(r.mapping.blocked)
        return out

    @property
    def total_duration(self) -> float:
        return self.finished - self.started if self.finished else 0.0

    @property
    def fields_filled(self) -> int:
        return sum(len(r.actions) for r in self.records)

    def to_dict(self) -> dict:
        return {
            "records": [r.to_dict() for r in self.records],
            "completed": self.completed,
            "failed": self.failed,
            "unverified_records": self.unverified,
            "unverified_fields": self.unverified_fields,
            "blocked_fields": [list(b) for b in self.blocked_fields],
            "total_duration": self.total_duration,
            "stopped_reason": self.stopped_reason,
        }


#: A panel whose structured scroll (pattern/wheel/drag/keyboard/override, all
#: verified) fails this many cycles IN A ROW gets a raw click-and-wheel
#: fallback forced on it regardless of what the structured methods reported -
#: see ``AgentLoop._scroll_one_container``. This is the last line of defence
#: against the panel ever going permanently idle.
_RAW_SCROLL_FAILSAFE_THRESHOLD = 2


class AgentLoop:
    """Runs the observe -> ... -> verify loop until records run out."""

    def __init__(
        self,
        target: TargetAdapter,
        source_reader: SourceReader,
        mapper: SemanticMapper,
        planner: ActionPlanner,
        executor: ActionExecutor,
        memory: Any | None = None,
        verify_after_action: bool = True,
        max_records: int = 0,
        next_record_timeout: float = 120.0,
        next_record_poll: float = 1.5,
        alias_learning: bool = False,
        scene_hook: Callable[[SceneDescription], SceneDescription] | None = None,
        on_record: Callable[[RecordResult], None] | None = None,
        field_map: UiaFieldMap | None = None,
        ocr_callback: Callable[[BBox], list[OcrText]] | None = None,
        debug_dir: str | Path | None = None,
        session_dir: str | Path | None = None,
        state_budget: float | dict[str, float] | None = None,
        record_builder: RecordBuilder | None = None,
        capture_callback: Callable[[Path], bool] | None = None,
        max_scan_rounds: int = 20,
        scan_reveal_fields: bool = False,
        settle_on_start: bool = False,
        scroll_stall_limit: int = 3,
        field_map_refresh: Callable[[], UiaFieldMap | None] | None = None,
        scroll_regions: Callable[[SceneDescription], list[BBox]] | None = None,
        scroll_container_provider: Callable[[], ScrollSession | None] | None = None,
        scroll_min_pixels: int = 250,
        scroll_max_pixels: int = 350,
        scroll_settle: tuple[float, float] = (0.3, 0.5),
        field_driven: bool = False,
        field_driven_scroll: bool = True,
        field_timeout: float = DEFAULT_FIELD_TIMEOUT,
        field_scroll_attempts: int = DEFAULT_SCROLL_ATTEMPTS,
        field_retries: int = DEFAULT_FIELD_RETRIES,
    ) -> None:
        self._target = target
        self._source_reader = source_reader
        self._mapper = mapper
        self._planner = planner
        self._executor = executor
        self._memory = memory
        self._verify_after_action = verify_after_action
        self._max_records = max_records
        self._next_timeout = next_record_timeout
        self._next_poll = next_record_poll
        self._alias_learning = alias_learning
        self._scene_hook = scene_hook
        self._on_record = on_record
        self._field_map = field_map
        self._ocr_callback = ocr_callback
        self._debug_dir = Path(debug_dir) if debug_dir else None
        self._session_dir = Path(session_dir) if session_dir else (self._debug_dir / "session" if self._debug_dir else None)
        self._record_builder = record_builder or RecordBuilder()
        self._capture_callback = capture_callback
        self._max_scan_rounds = max_scan_rounds
        self._scan_reveal_fields = scan_reveal_fields
        self._settle_on_start = settle_on_start
        self._scroll_stall_limit = max(1, scroll_stall_limit)
        self._field_map_refresh = field_map_refresh
        self._scroll_regions = scroll_regions
        self._scroll_min_pixels = max(100, scroll_min_pixels)
        self._scroll_max_pixels = max(self._scroll_min_pixels, scroll_max_pixels)
        self._scroll_settle = scroll_settle
        self._scroll_container_provider = scroll_container_provider
        self._scroll_session: ScrollSession | None = None
        #: Field-driven (performance) path flags. When ``field_driven`` is set
        #: the loop fills from a single ordered UIA field-map queue and scrolls
        #: the RIGHT panel only, instead of the viewport-round reveal pass.
        self._field_driven = field_driven
        self._field_driven_scroll = field_driven_scroll
        self._field_timeout = max(1.0, float(field_timeout))
        self._field_scroll_attempts = max(1, int(field_scroll_attempts))
        self._field_retries = max(0, int(field_retries))
        #: Consecutive scroll-method failures per panel (LEFT/RIGHT). When a
        #: panel's structured scroll (pattern/wheel/drag/keyboard/override)
        #: fails this many cycles IN A ROW, `_scroll_one_container` forces a
        #: raw click-and-wheel fallback on the panel's own rect regardless of
        #: what the structured methods think, so the panel can never go
        #: permanently idle. See `_RAW_SCROLL_FAILSAFE_THRESHOLD`.
        self._panel_scroll_failures: dict[str, int] = {}
        self._state_budget = self._normalize_budget(state_budget)
        self._states = StateMachine()
        self._stop = False
        self._pause = False
        self._last_layout = ""
        self._state_entered: dict[AgentState, float] = {}
        self._state_warned: set[AgentState] = set()
<<<<<<< HEAD
        #: Consecutive overrun ticks per state so the level-2 watchdog
        #: escalates instead of logging once and going silent while the loop
        #: keeps spinning inside the same stuck state. Reset on `_set`.
        self._state_overruns: dict[AgentState, int] = {}
        self._last_overrun_log: dict[AgentState, float] = {}
        self._overrun_repeat_log_seconds: float = 30.0
=======
>>>>>>> 506caa78300fd5640f3fd0dcb51ac6f142dcd8ca
        self._bus = get_event_bus()
        self._cached_analysis: SceneAnalysis | None = None
        self._last_signature = ""
        self._force_rebuild = False
        self._last_field: str | None = None
        self._planner_status = ""
        self._last_exception: str | None = None
        self._no_record_last_reason = ""
        self._expanded_sections: set[str] = set()
        self._scroll_position: int = 0
        self._scroll_blocked_reason: str | None = None
        #: The field-driven queue currently being filled, used to refresh an
        #: action's bbox right before verification (stable-id -> live position).
        self._active_queue: PendingFieldQueue | None = None
        if self._executor is not None:
            self._executor.set_bbox_refresher(self.refresh_action_bbox)

    # -- lifecycle -----------------------------------------------------------

    @property
    def state(self) -> AgentState:
        return self._states.state

    def stop(self) -> None:
        self._stop = True

    def pause(self) -> None:
        self._pause = True

    def resume(self) -> None:
        self._pause = False

    def run(self) -> WorkflowSummary:
        summary = WorkflowSummary()
        self._states.reset()
        self._bus.publish(EventType.AGENT_STARTED)
        try:
            if not self._target.is_alive():
                raise RuntimeError("target is not attached")
            # Startup: a human looks at the form, waits for it to finish
            # rendering, and only then starts working. Never scroll yet.
            self._wait_until_stable()
            count = 0
            last_key: str | None = None
            while not self._stop:
                self._check_state_budget()
                if self._max_records and count >= self._max_records:
                    summary.stopped_reason = f"max_records reached ({count})"
                    break
                if self._pause:
                    time.sleep(0.2)
                    continue
                awaited = self._await_record(last_key)
                if awaited is None:
                    # Only reached when the loop was stopped; never on records==0.
                    break
                analysis, record = awaited
                if self._field_driven:
                    result = self._run_record_field_driven(analysis, record, count + 1)
                else:
                    result = self._run_record(analysis, record, count + 1)
                summary.records.append(result)
                count += 1
                if self._on_record is not None:
                    try:
                        self._on_record(result)
                    except Exception:
                        logger.exception("on_record callback failed")
                last_key = result.record.record_key
                self._bus.publish(
                    EventType.RECORD_COMPLETED if result.success else EventType.RECORD_FAILED,
                    result.to_dict(),
                )
                # After an upload the left panel changes to the next record:
                # force the screen model to rebuild on the next observation.
                self._force_rebuild = True
        except Exception as exc:
            logger.exception("workflow failed")
            summary.stopped_reason = str(exc)
            self._last_exception = str(exc)
        finally:
            summary.finished = time.time()
            if self._stop:
                summary.stopped_reason = summary.stopped_reason or "stopped by user"
            self._states.transition(AgentState.STOPPED)
            self._bus.publish(EventType.WORKFLOW_COMPLETE, summary.to_dict())
            self._bus.publish(EventType.AGENT_STOPPED, {"reason": summary.stopped_reason})
            self._dump_timeline(summary)
            self._dump_failure(summary)
            self._dump_focus_history()
<<<<<<< HEAD
            self._dump_watchdog()
=======
>>>>>>> 506caa78300fd5640f3fd0dcb51ac6f142dcd8ca
            self._dump_verification_debug(summary)
            self._dump_run_metrics(summary)
        return summary

    # -- record processing ----------------------------------------------------

    def _run_record(self, analysis: SceneAnalysis, record: SourceRecord, index: int) -> RecordResult:
        with Timer() as timer:
            scene = analysis.scene
            self._set(AgentState.SCREEN_MODEL)
            self._set(AgentState.RECORD_EXTRACTION)
            self._bus.publish(EventType.SOURCE_READ, record.to_dict())
            self._bus.publish(EventType.RECORD_STARTED, {"index": index, "record": record.to_dict()})

            # Scroll-locked observation of the current viewport: the whole
            # visible page is read before the first field is touched.
            self._set(AgentState.OBSERVE_VIEWPORT)
            if self._scan_reveal_fields:
                self._lock_scrolling()

            fields = discover_fields(scene)
            if self._scan_reveal_fields:
                # A human scans the current viewport first: below-fold fields are
                # filled later by the reveal pass, so the initial plan must never
                # act on stale geometry it cannot see.
                all_fields = fields
                fields = self._visible_fields(all_fields, self._viewport(analysis))
            else:
                all_fields = fields
            self._bus.publish(
                EventType.FIELD_DISCOVERED, {
                    "count": len(all_fields),
                    "visible": len(fields),
                    "fields": [f.to_dict() for f in fields],
                }
            )

            self._set(AgentState.FIELD_MAPPING)
            mapping = self._mapper.map(record, fields)
            self._bus.publish(EventType.MAPPING, mapping.to_dict())

            submit_id = self._find_submit(scene)
            self._set(AgentState.PLANNING)
            plan = self._planner.plan_fill(record, mapping, scene, submit_id)
            # When the reveal pass owns the scroll, defer the submit action to
            # the very end so an early click never fires while fields below the
            # fold are still unfilled.
            defer_submit = bool(
                self._scan_reveal_fields
                and submit_id is not None
                and any(self._is_submit_action(a, submit_id) for a in plan.actions)
            )
            if defer_submit:
                plan.actions = [a for a in plan.actions if not self._is_submit_action(a, submit_id)]
            self._bus.publish(EventType.PLAN_CREATED, plan.to_dict())
            self._planner_status = f"{len(plan.actions)} actions planned"

        self._set(AgentState.THINKING)
        key = record.record_key or ""
        self._snapshot("before-fill", index, key)
        self._dump_record_debug(plan, [], index, record)
        results = self._execute_plan(plan, submit_id, index=index, record_key=key)
        # Reveal and fill fields that were below the fold in the first observe
        # (vision targets only see the viewport). Opt-in: the re-observe consumes
        # a fresh screen, which is correct only for live target adapters that
        # return the current screen (idempotent), not sequential test mocks.
        # Bounded; never loops.
        if self._scan_reveal_fields:
            handled_ids = {f.element_id for f in fields}
            handled_labels = {m.source_label for m in mapping.mappings}
            extra = self._scan_fill_revealed(
                record, handled_ids, handled_labels=handled_labels,
                submit_id=submit_id if defer_submit else None, index=index,
                initial_scene=scene,
            )
            if extra:
                results = results + extra
        if not self._all_ok(results):
            self._snapshot("failure", index, key)
        self._snapshot("after-fill", index, key)
        self._dump_record_debug(plan, results, index, record)
        self._bus.publish(
            EventType.SCREEN_STATE,
            build_screen_state(
                scene=scene,
                record=record,
                mapping=mapping,
                results=results,
                window_title=getattr(getattr(self._target, "info", None), "title", "") or "",
                record_index=index,
            ),
        )

        result = RecordResult(
            index=index,
            record=record,
            mapping=mapping,
            actions=results,
            success=self._all_ok(results),
            duration_ms=timer.elapsed * 1000.0,
        )
        self._learn_aliases(record, mapping, results)
        result.incomplete_fields = self._unmapped_required(mapping)
        result.skipped_fields = self._skipped_fields(results)
        result.unverified_fields = self._unverified_fields(results)
        if result.skipped_fields or result.incomplete_fields:
            result.message = (
                f"skipped {len(result.skipped_fields)} field(s), "
                f"{len(result.incomplete_fields)} required unmapped"
            )
        if result.unverified_fields:
            suffix = f"; {len(result.unverified_fields)} field(s) written but NOT verified (UNKNOWN)"
            result.message = f"{result.message}{suffix}" if result.message else suffix[2:]
        logger.info(
            "record {} ({}) -> {} in {:.1f}s",
            index,
            record.record_key or "?",
            "OK" if result.success else "FAILED",
            result.duration_ms / 1000.0,
        )
        return result

    # -- field-driven processing (performance path) --------------------------

    def _run_record_field_driven(self, analysis: SceneAnalysis, record: SourceRecord, index: int) -> RecordResult:
        """Fill a record from the ordered UIA field-map queue (performance path).

        Unlike the viewport-round model, the whole form's fields (including
        below-fold ones) come from the UIA field map as ONE ordered queue. The
        loop walks the queue, scrolling the RIGHT panel only to reach below-fold
        targets, and refreshes positions via UIA (no VLM). A single full VLM
        observe is taken after submit to confirm the success indicator / record
        change.
        """
        with Timer() as timer:
            self._set(AgentState.SCREEN_MODEL)
            self._set(AgentState.RECORD_EXTRACTION)
            self._bus.publish(EventType.SOURCE_READ, record.to_dict())
            self._bus.publish(EventType.RECORD_STARTED, {"index": index, "record": record.to_dict()})

            self._refresh_field_map_once()
            field_map = self._field_map
            if field_map is None or not field_map.has_form:
                logger.warning("field-driven path needs a UIA field map; falling back to viewport path")
                return self._run_record(analysis, record, index)

            queue = build_field_queue(field_map, record)
            if not queue.items:
                logger.warning("field-driven path found no fillable fields; falling back to viewport path")
                return self._run_record(analysis, record, index)
<<<<<<< HEAD
            order_ok, bad_at = queue.validate_order()
            if not order_ok:
                logger.warning(
                    "record {}: initial field queue OUT OF reading order at index {} ({} fields)",
                    index,
                    bad_at,
                    len(queue.items),
                )

            perf = PerfTracker()
            key = record.record_key or ""
            timings: list[dict] = []
            self._snapshot("before-fill", index, key)
            results = self._fill_from_queue(queue, index, perf, record, timings)

            # Two-pass fill: after the first sequential walk, re-run every
            # source-backed field that failed or was left pending. Cascading
            # dependents (Sub Caste after Caste, Nakshatra after DOB, ...) that
            # were disabled on the first visit are now enabled by their parent
            # and can be filled on the recovery pass.
            blockers = queue.blockers()
            if blockers:
                logger.info(
                    "record {}: completeness pass over {} source-backed field(s)",
                    index,
                    len(blockers),
                )
                self._snapshot("before-retry", index, key)
                for target in list(queue.items):
                    if target.source_backed and (target.failed or target.status is FieldStatus.RETRY_PENDING):
                        queue.mark_status(target, FieldStatus.RETRY_PENDING, "completeness pass retry")
                        target.retries = 0
                extra = self._fill_from_queue(queue, index, perf, record, timings)
                results.extend(extra)
            self._snapshot("after-fill", index, key)

            submit_ok: bool | None = None
            blockers = queue.blockers()
            if not blockers and queue.failed == 0 and self._field_map is not None:
=======

            perf = PerfTracker()
            key = record.record_key or ""
            self._snapshot("before-fill", index, key)
            results = self._fill_from_queue(queue, index, perf)
            self._snapshot("after-fill", index, key)

            submit_ok: bool | None = None
            if queue.all_ok() and self._field_map is not None:
>>>>>>> 506caa78300fd5640f3fd0dcb51ac6f142dcd8ca
                perf.start("submit")
                submit = self._submit_field_driven(record, index)
                perf.stop("submit")
                if submit is not None:
                    results.append(submit)
                    submit_ok = submit.ok
<<<<<<< HEAD
            elif blockers:
                logger.warning(
                    "record {}: submit BLOCKED - {} source-backed field(s) not safely filled",
                    index,
                    len(blockers),
                )
            elif queue.failed:
                logger.warning("record {}: {} field(s) failed; submit skipped", index, queue.failed)

            self._write_field_perf(perf, queue, index, record, timings)

        success = bool(not queue.blockers() and queue.failed == 0 and submit_ok is not False)
=======
            elif queue.failed:
                logger.warning("record {}: {} field(s) failed; submit skipped", index, queue.failed)

            self._write_field_perf(perf, queue, index, record)

        success = bool(queue.all_ok() and submit_ok is not False)
>>>>>>> 506caa78300fd5640f3fd0dcb51ac6f142dcd8ca
        result = RecordResult(
            index=index,
            record=record,
            mapping=MappingResult(),
            actions=results,
            success=success,
            duration_ms=timer.elapsed * 1000.0,
        )
        result.skipped_fields = self._skipped_fields(results)
        result.unverified_fields = self._unverified_fields(results)
<<<<<<< HEAD
        parts = []
        blockers = queue.blockers()
        if blockers:
            parts.append(f"{len(blockers)} source-backed field(s) not safely filled")
        if queue.failed:
            parts.append(f"{queue.failed} field(s) failed")
        if result.skipped_fields:
            parts.append(f"{len(result.skipped_fields)} skipped")
        result.message = "; ".join(parts)
        if result.unverified_fields:
            suffix = f"{len(result.unverified_fields)} field(s) written but NOT verified (UNKNOWN)"
            result.message = f"{result.message}; {suffix}" if result.message else suffix
        logger.info(
            "record {} ({}) -> {} in {:.1f}s (field-driven) | statuses={}",
=======
        if result.skipped_fields or queue.failed:
            result.message = f"{queue.failed} field(s) failed, {len(result.skipped_fields)} skipped"
        if result.unverified_fields:
            suffix = f"; {len(result.unverified_fields)} field(s) written but NOT verified (UNKNOWN)"
            result.message = f"{result.message}{suffix}" if result.message else suffix[2:]
        logger.info(
            "record {} ({}) -> {} in {:.1f}s (field-driven)",
>>>>>>> 506caa78300fd5640f3fd0dcb51ac6f142dcd8ca
            index,
            record.record_key or "?",
            "OK" if success else "FAILED",
            result.duration_ms / 1000.0,
<<<<<<< HEAD
            queue.status_summary(),
=======
>>>>>>> 506caa78300fd5640f3fd0dcb51ac6f142dcd8ca
        )
        return result

    def _refresh_field_map_once(self) -> None:
        """Re-query the UIA field map (no VLM) so the queue sees fresh geometry."""
        if self._field_map_refresh is None:
            return
        try:
            refreshed = self._field_map_refresh()
            if refreshed is not None:
                self._field_map = refreshed
        except Exception as exc:
            logger.debug("field map refresh failed: {}", exc)

    def _field_scroll_session(self) -> ScrollSession | None:
        """Discover the UIA scroll containers once for the field-driven path."""
        if self._scroll_container_provider is None:
            self._scroll_session = None
            return None
        try:
            session = self._scroll_container_provider()
        except Exception as exc:
            logger.debug("scroll container discovery failed: {}", exc)
            session = None
        self._scroll_session = session if (session is not None and session.available) else None
        return self._scroll_session

    def _field_right_container(self, session: ScrollSession | None) -> Any | None:
        """The RIGHT (entry form) scroll container from the discovered session."""
        if session is None or not session.available:
            return None
        field_map = self._field_map
        try:
            chosen = pick_left_right_containers(
                session.containers,
                field_map.left_rect if field_map is not None else None,
                field_map.right_rect if field_map is not None else None,
                self._client_rect(),
            )
        except Exception as exc:
            logger.debug("field-driven container match failed: {}", exc)
            return None
        return chosen.get(PANEL_RIGHT)

    def _field_fill_viewport(
        self,
        session: ScrollSession | None,
        client_rect: tuple[int, int, int, int] | None,
    ) -> tuple[int, int, int, int] | None:
        """The entry form's visible band for fill decisions, or None.

        The RIGHT panel is a UIA scroll container whose ``rect`` is the actual
        on-screen clip region. A fixed status/footer bar often sits just below
        it (the MPF "Record 114 of 114" line) and must never be mistaken for a
        field row - so fill/scroll decisions are made against the container's
        visible band, clipped to the window client rect, instead of the whole
        client area. Falls back to the full client rect when the container is
        unknown (tests / viewport path), preserving the old semantics there.
        """
        if client_rect is None:
            return None
        container = self._field_right_container(session) if session is not None else None
        rect = getattr(container, "rect", None)
        if rect is None:
            return client_rect
        left, top, right, bottom = client_rect
        v_left = max(left, rect.left)
        v_top = max(top, rect.top)
        v_right = min(right, rect.right)
        v_bottom = min(bottom, rect.bottom)
        if v_right <= v_left or v_bottom <= v_top:
            return client_rect
        return (v_left, v_top, v_right, v_bottom)

<<<<<<< HEAD
    def _fill_from_queue(
        self,
        queue: PendingFieldQueue,
        index: int,
        perf: PerfTracker,
        record: SourceRecord,
        timings: list[dict] | None = None,
    ) -> list[ActionResult]:
        """Walk the queue: fill visible targets, scroll RIGHT to reach below-fold ones.

        No field is ever dropped silently: a target with no source value is
        skipped with an explicit NO_SOURCE status (and a FIELD_SKIPPED log),
        and every fill finishes with an explicit status (VERIFIED / FILLED /
        ALREADY_CORRECT / FAILED / RETRY_PENDING).
        """
=======
    def _fill_from_queue(self, queue: PendingFieldQueue, index: int, perf: PerfTracker) -> list[ActionResult]:
        """Walk the queue: fill visible targets, scroll RIGHT to reach below-fold ones."""
>>>>>>> 506caa78300fd5640f3fd0dcb51ac6f142dcd8ca
        results: list[ActionResult] = []
        self._active_queue = queue
        client_rect = self._client_rect()
        guard = ProgressGuard(timeout=self._field_timeout)
        navigator = TargetNavigator(self._scroll_min_pixels, self._scroll_max_pixels)
        cache = ScrollCapabilityCache()
        session = self._field_scroll_session()
        viewport = self._field_fill_viewport(session, client_rect)
        scrolled: dict[str, int] = {}
<<<<<<< HEAD
        total = len(queue.items)
=======
>>>>>>> 506caa78300fd5640f3fd0dcb51ac6f142dcd8ca
        while not self._stop and queue.next_pending() is not None:
            self._check_state_budget()
            target = queue.next_pending()
            guard.begin()
<<<<<<< HEAD
            label = target.label or target.stable_id
            if not target.source_backed:
                self._mark_skipped(
                    queue, target, FieldStatus.NO_SOURCE, results,
                    f"no source value for {label}",
                )
                done_now = queue.done + queue.failed
                logger.info("[{:>2}/{}] {:<24} SKIP NO_SOURCE", done_now, total, label[:24])
                self._record_timing(timings, target, FieldStatus.NO_SOURCE, 0.0)
                continue
            if navigator.fillable(target, viewport):
                if self._wait_target_enabled(queue, target, guard, navigator, viewport):
                    t0 = time.time()
                    self._bus.publish(
                        EventType.ACTION_STARTED,
                        {"type": "FILL", "field_id": target.stable_id, "label": target.label},
                    )
=======
            if navigator.fillable(target, viewport):
                if self._wait_target_enabled(queue, target, guard, navigator, viewport):
>>>>>>> 506caa78300fd5640f3fd0dcb51ac6f142dcd8ca
                    perf.start("fill")
                    ok, action_results = self._fill_target(target, index)
                    perf.stop("fill")
                    results.extend(action_results)
<<<<<<< HEAD
                    elapsed = time.time() - t0
                    self._warn_field_latency(target, elapsed)
                    if ok:
                        status = classify_fill_status(action_results)
                        queue.mark_status(target, status, "")
                        done_now = queue.done + queue.failed
                        logger.info(
                            "[{:>2}/{}] {:<24} OK {:.1f}s [{}]",
                            done_now, total, label[:24], elapsed, status.value,
                        )
                        self._record_timing(timings, target, status, elapsed)
=======
                    if ok:
                        queue.mark_done(target)
>>>>>>> 506caa78300fd5640f3fd0dcb51ac6f142dcd8ca
                        continue
                    target.retries += 1
                    if target.retries > self._field_retries:
                        self._mark_failed(queue, target, results, "fill failed")
<<<<<<< HEAD
                        self._record_timing(timings, target, FieldStatus.FAILED, elapsed)
                        continue
                    queue.mark_status(target, FieldStatus.RETRY_PENDING, "fill failed once - retrying")
                    self._record_timing(timings, target, FieldStatus.RETRY_PENDING, elapsed)
                    self._refresh_and_merge(queue, record)
                    continue
                self._mark_failed(queue, target, results, "dependent field never enabled")
                self._record_timing(timings, target, FieldStatus.FAILED, 0.0)
=======
                        continue
                    self._refresh_field_map_once()
                    if self._field_map is not None:
                        queue.refresh_positions(self._field_map.right_fields)
                    continue
                self._mark_failed(queue, target, results, "dependent field never enabled")
>>>>>>> 506caa78300fd5640f3fd0dcb51ac6f142dcd8ca
                continue
            # Below the fold: scroll the RIGHT panel toward the target.
            if not self._field_driven_scroll:
                self._mark_failed(queue, target, results, "target below fold and scroll disabled")
<<<<<<< HEAD
                self._record_timing(timings, target, FieldStatus.FAILED, 0.0)
=======
>>>>>>> 506caa78300fd5640f3fd0dcb51ac6f142dcd8ca
                continue
            tries = scrolled.get(target.stable_id, 0)
            if tries >= self._field_scroll_attempts:
                self._mark_failed(queue, target, results, "target below fold after repeated scrolling")
<<<<<<< HEAD
                self._record_timing(timings, target, FieldStatus.FAILED, 0.0)
=======
>>>>>>> 506caa78300fd5640f3fd0dcb51ac6f142dcd8ca
                continue
            perf.start("scroll")
            moved = self._scroll_to_target(queue, target, viewport, session, cache, navigator, guard)
            perf.stop("scroll")
            scrolled[target.stable_id] = tries + 1
            if not moved and not navigator.fillable(target, viewport):
                self._mark_failed(queue, target, results, "could not scroll target into view")
<<<<<<< HEAD
                self._record_timing(timings, target, FieldStatus.FAILED, 0.0)
                continue
        return results

    @staticmethod
    def _record_timing(timings: list[dict] | None, target: Any, status: FieldStatus, elapsed: float) -> None:
        """Append one per-field timing row to the report collector."""
        if timings is None:
            return
        timings.append({
            "stable_id": target.stable_id,
            "label": target.label or target.stable_id,
            "status": status.value,
            "elapsed": round(elapsed, 3),
        })

    @staticmethod
    def _warn_field_latency(target: Any, elapsed: float) -> None:
        """Surface a slow field so a drag never hides behind the progress logs."""
        label = target.label or target.stable_id
        if elapsed >= 5.0:
            logger.warning("VERY_SLOW_FIELD {:<24} {:.1f}s", label, elapsed)
        elif elapsed >= 3.0:
            logger.warning("SLOW_FIELD {:<24} {:.1f}s", label, elapsed)

    def _refresh_and_merge(self, queue: PendingFieldQueue, record: SourceRecord) -> None:
        """Refresh UIA positions AND fold newly discovered fields into the queue.

        The queue was built from the first snapshot; fields that only appear
        later (lazy render, dynamic sections) must join it or they are skipped
        forever. ``merge_fields`` appends them in reading order without
        disturbing the already-built deterministic order.
        """
        self._refresh_field_map_once()
        if self._field_map is None:
            return
        queue.refresh_positions(self._field_map.right_fields)
        added = queue.merge_fields(self._field_map.right_fields)
        if added:
            logger.info("field queue grew by {} field(s) after refresh", added)

=======
                continue
        return results

>>>>>>> 506caa78300fd5640f3fd0dcb51ac6f142dcd8ca
    def _wait_target_enabled(
        self,
        queue: PendingFieldQueue,
        target: Any,
        guard: ProgressGuard,
        navigator: TargetNavigator,
        viewport: tuple[int, int, int, int] | None,
    ) -> bool:
        """Wait for a cascading/dependent field to become enabled (bounded).

        A dependent dropdown (District after State, Taluk after District,
        Caste after Religion, Sub Caste after Caste, Nakshatra after DOB, ...)
        starts DISABLED until its parent is filled. Refreshing only the UIA
        positions (no VLM) lets the live ``enabled`` state propagate into the
        queue's nodes, with adaptive polling that backs off as the wait drags
        on. Bounded by the per-field progress guard so a genuinely dead field
        is failed quickly instead of stalling the record.
        """
        if target.enabled:
            return True
        logger.info(
            "field {} is disabled - waiting for dependent parent to enable it",
            target.stable_id,
        )
        intervals = (0.15, 0.25, 0.4, 0.6, 0.9)
        idx = 0
        # The dependent-dropdown budget (<5s) is much tighter than the general
        # per-field guard, so a dead disabled field fails fast instead of
        # stalling the record.
        deadline = time.time() + min(5.0, self._field_timeout)
        while not self._stop and not guard.expired and time.time() < deadline:
<<<<<<< HEAD
            # NOTE: no early-return when the target scrolls away mid-wait. The
            # fill loop never scrolls during this wait, so a target that
            # entered fillable stays fillable; returning True on a non-fillable
            # target used to let a STILL-DISABLED field be marked done with no
            # actions (a silent skip of the very dependent combos this wait
            # exists to protect).
=======
            if not navigator.fillable(target, viewport):
                return True  # scrolled away; handled by the scroll branch
>>>>>>> 506caa78300fd5640f3fd0dcb51ac6f142dcd8ca
            time.sleep(intervals[idx])
            idx = min(idx + 1, len(intervals) - 1)
            self._refresh_field_map_once()
            if self._field_map is not None:
                queue.refresh_positions(self._field_map.right_fields)
            if target.enabled:
                return True
        return target.enabled

    def _scroll_to_target(
        self,
        queue: PendingFieldQueue,
        target: Any,
        viewport: tuple[int, int, int, int] | None,
        session: ScrollSession | None,
        cache: ScrollCapabilityCache,
        navigator: TargetNavigator,
        guard: ProgressGuard,
    ) -> bool:
        """Scroll the RIGHT panel toward a below-fold target using the cached method.

        Verifies progress via container percent / target-y change (no full VLM
        re-observe). Returns True when the target became fillable or moved.
        """
        container = self._field_right_container(session)
        if container is None:
            return self._wheel_fallback_to_target(queue, target, viewport, navigator, guard)
        amount = navigator.scroll_amount_for(target, viewport)
        before = ScrollProgress.capture(container, target)
        self._set(AgentState.SCROLLING, "SCROLLING FORM (FIELD-DRIVEN)")
        scroller = getattr(session, "scroller", None)
        backend = getattr(scroller, "_backend", None)
        dom_available = getattr(scroller, "_dom", None) is not None
        info = getattr(self._target, "info", None)
        handle = getattr(info, "handle", None)
        method = cache.method_for(container, dom_available=dom_available)

        for _ in range(self._field_scroll_attempts):
            if guard.expired:
                break
            fn = make_scroll_fn(session, method, handle=handle, backend=backend)
            moved = bool(fn(container, amount)) if fn is not None else False
            if not moved:
                outcome = None
                if scroller is not None:
                    try:
                        outcome = scroller.scroll_down(container, amount, verify=lambda: False)
                    except Exception as exc:
                        logger.debug("escalation scroll failed: {}", exc)
                method = outcome.method if outcome is not None else "none"
                moved = bool(outcome is not None and outcome.method != "none")
            if method != "none":
                cache.remember(container, method)
            if moved and backend is not None and getattr(container, "has_scroll_pattern", False):
                try:
                    backend.container_state(container, handle)
                except Exception:
                    pass
            time.sleep(random.uniform(*self._scroll_settle))
            self._refresh_field_map_once()
            if self._field_map is not None:
                queue.refresh_positions(self._field_map.right_fields)
<<<<<<< HEAD
                queue.merge_fields(self._field_map.right_fields)
=======
>>>>>>> 506caa78300fd5640f3fd0dcb51ac6f142dcd8ca
            if navigator.fillable(target, viewport) or before.moved(container, target):
                return True
            before = ScrollProgress.capture(container, target)
        return navigator.fillable(target, viewport)

    def _wheel_fallback_to_target(
        self,
        queue: PendingFieldQueue,
        target: Any,
        viewport: tuple[int, int, int, int] | None,
        navigator: TargetNavigator,
        guard: ProgressGuard,
    ) -> bool:
        """No UIA scroll container: click-focus + wheel the RIGHT panel rect."""
        field_map = self._field_map
        rect = field_map.right_rect if field_map is not None else None
        if rect is None:
            return False
        amount = navigator.scroll_amount_for(target, viewport)
        self._set(AgentState.SCROLLING, "SCROLLING FORM (FIELD-DRIVEN)")
        for _ in range(self._field_scroll_attempts):
            if guard.expired:
                break
            self._scroll_region(PANEL_RIGHT, rect, amount, reason="scroll right panel toward field")
            time.sleep(random.uniform(*self._scroll_settle))
            self._refresh_field_map_once()
            if self._field_map is not None:
                queue.refresh_positions(self._field_map.right_fields)
<<<<<<< HEAD
                queue.merge_fields(self._field_map.right_fields)
=======
>>>>>>> 506caa78300fd5640f3fd0dcb51ac6f142dcd8ca
            if navigator.fillable(target, viewport):
                return True
        return navigator.fillable(target, viewport)

    def refresh_action_bbox(self, field_id: str | None) -> BBox | None:
        """Live bbox for an action by stable-id match against the active queue.

        Called by the executor just before verification so a read never uses a
        bbox made stale by a scroll or window resize since the write. Returns
        None outside the field-driven path (the viewport path re-observes
        instead), so no stale geometry is ever substituted there.
        """
        if field_id is None or self._active_queue is None:
            return None
        try:
            return self._active_queue.bbox_for_id(field_id)
        except Exception:
            return None

    def _fill_target(self, target: Any, index: int) -> tuple[bool, list[ActionResult]]:
        """Execute the fill actions for one target; returns (ok, action_results)."""
        actions = build_field_actions(target)
        results: list[ActionResult] = []
        for action in actions:
            if self._stop:
                break
            self._last_field = action.field_id or action.reason
            self._set(
                ACTION_STATE.get(action.type, AgentState.THINKING),
                ACTION_DETAIL.get(action.type),
            )
            self._bus.publish(EventType.ACTION_STARTED, action.to_dict())
            result = self._executor.execute(action)
            results.append(result)
            if not result.ok:
                logger.warning("field fill action failed: {} ({})", action.reason, result.message)
                return False, results
        # Whole-group verification for date triplets: after the per-part fills,
        # one combined read over Day+Month+Year confirms the date as a whole.
        # The date-aware verifier lets any spelling match (source ISO "1996-02-02"
        # vs. the triplet's "02 02 1996"), and the union bbox is refreshed live.
        if isinstance(target, DateGroupTarget) and getattr(target, "date_value", None):
            group_bbox = target.bbox
            if group_bbox is not None:
                verify = Action(
                    type=ActionType.VERIFY,
                    field_id=target.stable_id,
                    bbox=group_bbox,
                    value=target.date_value,
                    expected=target.date_value,
                    reason="verify DOB date group",
                )
                if not self._stop:
                    self._last_field = verify.field_id
                    self._set(AgentState.VERIFYING, "VERIFYING")
                    self._bus.publish(EventType.ACTION_STARTED, verify.to_dict())
                    result = self._executor.execute(verify)
                    results.append(result)
                    if not result.ok:
                        logger.warning("field fill action failed: {} ({})", verify.reason, result.message)
                        return False, results
        return all(r.ok for r in results), results

    def _mark_failed(self, queue: PendingFieldQueue, target: Any, results: list[ActionResult], reason: str) -> None:
        queue.mark_failed(target, reason)
        logger.warning("field {} failed: {}", getattr(target, "stable_id", target), reason)
        results.append(ActionResult(
            action=Action(type=ActionType.STOP, reason=reason),
            success=False,
            verified=False,
            message=reason,
        ))

<<<<<<< HEAD
    def _mark_skipped(
        self,
        queue: PendingFieldQueue,
        target: Any,
        status: FieldStatus,
        results: list[ActionResult],
        reason: str,
    ) -> None:
        """Record a deliberate skip with an explicit status and reason.

        Every skip is surfaced: logged (FIELD_SKIPPED), stored on the queue's
        ``skipped_items`` for the completeness report, and appended to the
        record's action results so the summary can account for it.
        """
        queue.mark_skipped(target, status, reason)
        logger.info(
            "FIELD_SKIPPED {}: {} [{}]",
            getattr(target, "stable_id", target),
            reason,
            status.value,
        )
        results.append(ActionResult(
            action=Action(type=ActionType.STOP, reason=reason, field_id=getattr(target, "stable_id", None)),
            success=True,
            verified=False,
            verification_status=status.value,
            message=reason,
        ))

=======
>>>>>>> 506caa78300fd5640f3fd0dcb51ac6f142dcd8ca
    def _submit_field_driven(self, record: SourceRecord, index: int) -> ActionResult | None:
        """Click the upload button once, then verify with a single VLM observe."""
        field_map = self._field_map
        if field_map is None or field_map.upload_button is None or field_map.upload_button.rect is None:
            logger.warning("no upload button in field map; submit skipped")
            return None
        button = field_map.upload_button
        action = Action(
            type=ActionType.CLICK,
            field_id=f"uia-btn-{button.handle or 'upload'}",
            bbox=button.rect,
            confidence=1.0,
            expected="clicked submit",
            reason="click submit button after filling the form",
        )
        self._bus.publish(EventType.UPLOADING, action.to_dict())
        self._set(AgentState.UPLOADING, "UPLOADING")
        self._last_field = action.field_id or action.reason
        result = self._executor.execute(action)
        if result.ok:
            self._bus.publish(EventType.UPLOAD_COMPLETED, result.to_dict())
            self._snapshot("after-upload", index, record.record_key or "")
        result.success = result.ok and self._verify_submit(record)
        return result

    def _verify_submit(self, record: SourceRecord) -> bool:
        """One VLM re-observe: record-key change or success text proves the submit."""
        self._force_rebuild = True
        try:
            analysis, _ = self._observe()
        except Exception as exc:
            logger.debug("post-submit observe failed: {}", exc)
            analysis = None
        if analysis is None:
            return True
        scene = analysis.scene
        try:
            next_record = self._extract_record(scene)
        except Exception as exc:
            logger.debug("post-submit record read failed: {}", exc)
            next_record = None
        if next_record is not None and next_record.record_key and next_record.record_key != record.record_key:
            return True
        text = " ".join(
            f"{e.label or ''} {e.name or ''}".lower()
            for e in scene.elements
        )
        if any(token in text for token in (
            "success", "submitted", "saved successfully", "record created",
            "upload successful", "record saved", "data saved",
        )):
            return True
        return not any(token in text for token in ("error", "failed", "validation error", "invalid", "cannot be blank"))

<<<<<<< HEAD
    def _write_field_perf(
        self, perf: PerfTracker, queue: PendingFieldQueue, index: int, record: SourceRecord,
        timings: list[dict] | None = None,
    ) -> None:
        coverage = field_coverage_summary(queue)
        logger.info(
            "record {}: targets={} mapped={} ({:.0%}) statuses={} skipped={} failed={}",
            index,
            coverage["total_targets"],
            coverage["mapped_targets"],
            coverage["mapped_pct"],
            queue.status_summary(),
            len(queue.skipped_items),
            queue.failed,
        )
=======
    def _write_field_perf(self, perf: PerfTracker, queue: PendingFieldQueue, index: int, record: SourceRecord) -> None:
>>>>>>> 506caa78300fd5640f3fd0dcb51ac6f142dcd8ca
        if self._debug_dir is None:
            return
        self._write_debug("field_driven_perf.json", {
            "record_index": index,
            "key": record.record_key,
            "phases": perf.to_dict(),
<<<<<<< HEAD
            "coverage": coverage,
            "timings": timings or [],
=======
>>>>>>> 506caa78300fd5640f3fd0dcb51ac6f142dcd8ca
            "queue": {
                "total": len(queue.items),
                "done": queue.done,
                "failed": queue.failed,
                "remaining": queue.remaining,
<<<<<<< HEAD
                "skipped": len(queue.skipped_items),
                "statuses": queue.status_summary(),
                "blockers": [
                    {
                        "label": it.label,
                        "status": it.status.value,
                        "reason": it.status_reason,
                    }
                    for it in queue.blockers()
                ],
=======
>>>>>>> 506caa78300fd5640f3fd0dcb51ac6f142dcd8ca
            },
        })

    def _execute_plan(
        self, plan: FillPlan, submit_element_id: str | None = None,
        index: int = 0, record_key: str = "",
    ) -> list[ActionResult]:
        results: list[ActionResult] = []
        for action in plan.actions:
            if self._stop:
                break
            self._check_state_budget()
            is_upload = action.type == ActionType.SUBMIT or (
                action.type == ActionType.CLICK and action.field_id == submit_element_id
            )
            if is_upload:
                self._bus.publish(EventType.UPLOADING, action.to_dict())
                self._set(AgentState.UPLOADING, "UPLOADING")
            else:
                self._set(
                    ACTION_STATE.get(action.type, AgentState.THINKING),
                    ACTION_DETAIL.get(action.type),
                )
            self._last_field = action.field_id or action.reason
            self._bus.publish(EventType.ACTION_STARTED, action.to_dict())
            result = self._executor.execute(action)
            results.append(result)
            if result.ok and is_upload:
                self._bus.publish(EventType.UPLOAD_COMPLETED, result.to_dict())
                self._snapshot("after-upload", index, record_key)
            if not result.ok and action.type in {ActionType.SUBMIT, ActionType.CLICK}:
                logger.warning("submit/click failed; stopping record: {}", result.message)
                break
        return results

    def _remaining_record(self, record: SourceRecord, handled_labels: set[str]) -> SourceRecord:
        """A copy of the source record minus the labels already written.

        The reveal pass maps only these onto freshly revealed fields, so each
        source value is typed exactly once into its own field instead of being
        re-bound to the next revealed control.
        """
        ordered = [label for label in record.ordered_labels if label not in handled_labels]
        return SourceRecord(
            pairs={label: record.pairs[label] for label in ordered if label in record.pairs},
            ordered_labels=ordered,
            title=record.title,
        )

    def _refresh_source_record(self, record: SourceRecord, scene: SceneDescription) -> SourceRecord:
        """Merge freshly-visible LEFT-panel source data into the working record.

        The left source panel scrolls together with the right entry form, so
        every scroll reveals more label:value rows. Without re-reading them,
        the fields revealed below the fold would have no value to fill. New
        labels are merged into the record; already-written labels are kept out.
        """
        try:
            pairs = self._collect_source_pairs(scene)
        except Exception as exc:
            logger.debug("left-panel source re-read failed: {}", exc)
            return record
        merged = dict(record.pairs)
        ordered = list(record.ordered_labels)
        changed = False
        for label, value in pairs:
            if label and label not in merged:
                merged[label] = value
                ordered.append(label)
                changed = True
        if not changed:
            return record
        return SourceRecord(pairs=merged, ordered_labels=ordered, title=record.title)

    def _scan_fill_revealed(
        self,
        record: SourceRecord,
        handled_ids: set[str],
        handled_labels: set[str] | None = None,
        submit_id: str | None = None,
        index: int = 0,
        initial_scene: SceneDescription | None = None,
    ) -> list[ActionResult]:
        """Fill-visible -> expand sections -> dual-panel scroll -> repeat.

        Treats the form as ONE continuous document with two independent
        scrollable panels (left source data + right entry form), exactly like a
        human operator:

        1. read    - re-read the LEFT source panel every round: each scroll
                     reveals more source rows whose values the newly revealed
                     fields below the fold need;
        2. scan    - fill every visible, unhandled field that has a value
                     (never skip a field that is on screen);
        3. expand  - click collapsible upload/attachment section headers so
                     their fields are revealed (never the submit button);
        4. scroll  - only once the current viewport is fully handled, scroll
                     BOTH panels a small incremental amount (click-focus inside
                     each panel first) and keep them synchronized;
        5. end     - stop ONLY when BOTH panels have reached their bottom
                     (i.e. no more content can be revealed on either side),
                     confirmed by the Upload Details section when present.

        The loop NEVER stops just because the current viewport is complete:
        it keeps scrolling as long as either panel still has content to reveal
        (live targets refresh their geometry after every scroll via
        ``field_map_refresh``). Bounded by ``max_scan_rounds``; never runs
        forever.
        """
        scroll_ctrl = DualPanelScroll(
            stall_limit=self._scroll_stall_limit,
            min_pixels=self._scroll_min_pixels,
            max_pixels=self._scroll_max_pixels,
            settle_range=self._scroll_settle,
        )
        rounds = max(1, self._max_scan_rounds)
        # Seed the scroll-progress baseline with the pre-reveal viewport: a
        # field that becomes visible on the FIRST reveal observation is already
        # scroll progress (the scroll before this pass moved it into view), so
        # the panel must count as "ever moved" from the very first round.
        if initial_scene is not None:
            scroll_ctrl.record_observation(initial_scene)
        # NEVER REVERSE SCROLL: the reveal pass is DOWN-only. While it runs,
        # the executor's scroll-into-view is forced DOWN so a failed scroll is
        # retried forward, never by scrolling back up.
        self._scroll_direction = "down"
        self._executor.set_scroll_direction("down")
        try:
            return self._scan_fill_revealed_rounds(
                record, handled_ids, handled_labels, submit_id, index, scroll_ctrl, rounds,
            )
        finally:
            self._executor.set_scroll_direction(None)
            self._scroll_direction = None

    def _scan_fill_revealed_rounds(
        self,
        record: SourceRecord,
        handled_ids: set[str],
        handled_labels: set[str] | None,
        submit_id: str | None,
        index: int,
        scroll_ctrl: DualPanelScroll,
        rounds: int,
    ) -> list[ActionResult]:
        results: list[ActionResult] = []
        for _round in range(rounds):
            if self._stop:
                break
            self._set(AgentState.OBSERVE_VIEWPORT)
            self._force_rebuild = True
            analysis, _ = self._observe()
            if analysis is None:
                break
            scene = analysis.scene
            viewport = self._viewport(analysis)

            # 1) read the LEFT source panel again: a scroll reveals more source
            #    rows, so lower fields always have a value to fill.
            self._set(AgentState.RECORD_EXTRACTION, "READING")
            record = self._refresh_source_record(record, scene)

            # 2) expand collapsed upload/section areas before filling.
            if self._expand_upload_section(scene, submit_id=submit_id, viewport=viewport):
                continue

            # 3) fill every visible, unhandled field that has a value.
            visible = self._visible_fields(discover_fields(scene), viewport)
            fresh = [f for f in visible if f.element_id not in handled_ids and f.bbox is not None]
            if fresh:
                # Only the source pairs not yet written to the form are mapped
                # onto the freshly revealed fields. Re-mapping the full record
                # would re-bind the first unfilled label (e.g. "Field 0") to
                # every new field revealed by a scroll and type its value again.
                handled_labels = handled_labels or set()
                pending = self._remaining_record(record, handled_labels)
                mapping = self._mapper.map(pending, fresh)
                sub_plan = self._planner.plan_fill(pending, mapping, scene, None)
                actionable = [a for a in sub_plan.actions if not self._is_submit_action(a, None)]
                if actionable:
                    self._lock_scrolling()
                    written_labels = {
                        m.target_id: m.source_label for m in mapping.mappings
                    }
                    for action in actionable:
                        if self._stop:
                            break
                        result = self._executor.execute(action)
                        results.append(result)
                        if action.field_id:
                            handled_ids.add(action.field_id)
                            if action.field_id in written_labels:
                                handled_labels.add(written_labels[action.field_id])
                    continue
                # These fields exist but have no value to fill: remember them so
                # we never re-scan them, then move on to scrolling.
                handled_ids.update(f.element_id for f in fresh)

            # 4) scroll permission: the LAST operation, only when the current
            #    viewport is complete (reveal-ready). Verification misses never
            #    block discovery (can_reveal_scroll, not can_scroll), so a
            #    single unverified value can never freeze the scan (Issue 1).
            if not self.can_reveal_scroll(scene, handled_ids, viewport, results, submit_id=submit_id):
                time.sleep(0.3)  # human-paced; bounded by max_scan_rounds
                continue

            # 5) track per-panel geometry + progress from the current scene so
            #    the completion checks know when a panel truly hit its bottom.
            #    When UIA scroll containers are available they are re-discovered
            #    and matched to the LEFT/RIGHT panels; their scroll percent then
            #    decides whether more content remains (never the viewport).
            self._refresh_scroll_session()
            containers = self._scroll_container_map(scroll_ctrl)
            scroll_ctrl.update_panels(self._panel_rects(scene), self._client_rect(), containers=containers)
            scroll_ctrl.record_observation(scene)

            # 6) completion: the spec's stop condition - Upload Details visible
            #    AND both panels reached their bottom AND no further scrolling
            #    is possible. Never stop just because the current viewport is
            #    complete - there are many fields below the fold on both sides.
            if scroll_ctrl.form_complete():
                logger.info("reveal pass done: {}", scroll_ctrl.completion_reason())
                break

            # 7) scroll BOTH panels a small incremental amount. When UIA
            #    containers are known each panel is scrolled *directly* through
            #    its own ScrollPattern (verified + retried), otherwise the prior
            #    click-focus wheel path is used. Panels stay synchronized.
            self._scroll_panels(analysis, scroll_ctrl)

        # 8) end of form: click submit/upload exactly once, never twice, and
        #    ONLY when the scan genuinely reached the bottom of both panels.
        #    A reveal pass that could not move any content (the MPF panels
        #    expose no ScrollPattern, so a failing scroll stalls without ever
        #    reaching the Upload Details section) must NOT submit - it would
        #    save a half-filled record. The failure is surfaced instead.
        #    Scrolling is unlocked BEFORE the click so the executor may scroll
        #    the button into view if it sits just below the fold, and it is
        #    ALWAYS released (finally) so the next record starts with full
        #    scroll freedom (the lock is re-engaged per record anyway).
        try:
            self._unlock_scrolling()
            if submit_id is not None:
                if scroll_ctrl.form_complete():
                    submit_result = self._click_submit_at_end(record, index=index)
                    if submit_result is not None:
                        results.append(submit_result)
                else:
                    results.append(ActionResult(
                        action=Action(
                            type=ActionType.STOP,
                            reason="form incomplete - Upload Details not reached",
                        ),
                        success=False,
                        verified=False,
                        message="form incomplete - Upload Details not reached; submit skipped",
                    ))
        finally:
            self._unlock_scrolling()
        return results

    def _click_submit_at_end(self, record: SourceRecord, index: int = 0) -> ActionResult | None:
        """Click the submit/upload button once after the form is fully handled.

        Re-observes so the click targets the current screen position (the loop
        may have scrolled since the button was first seen), then executes a
        single CLICK and surfaces the upload lifecycle events.
        """
        self._force_rebuild = True
        analysis, _ = self._observe()
        if analysis is None:
            return None
        scene = analysis.scene
        submit_id = self._find_submit(scene)
        if submit_id is None:
            return None
        element = scene.element(submit_id)
        if element is None or element.bbox is None:
            return None
        action = Action(
            type=ActionType.CLICK,
            field_id=submit_id,
            bbox=element.bbox.shifted(*scene.screen_offset),
            confidence=1.0,
            expected="clicked submit",
            reason="click submit button after filling the form",
        )
        self._bus.publish(EventType.UPLOADING, action.to_dict())
        self._set(AgentState.UPLOADING, "UPLOADING")
        self._last_field = action.field_id or action.reason
        result = self._executor.execute(action)
        if result.ok:
            self._bus.publish(EventType.UPLOAD_COMPLETED, result.to_dict())
            self._snapshot("after-upload", index, record.record_key or "")
        return result

    # -- viewport-driven scanning --------------------------------------------

    def _wait_until_stable(self, max_wait: float = 4.0, poll: float = 0.35) -> None:
        """Startup settle: wait for the attached UI to stop changing.

        Live targets re-render after attach (grids loading, panels painting);
        acting on a half-rendered form wastes the first viewport. Observations
        here are free and bounded, and the reveal pass guarantees no field is
        skipped. Stateless/mock targets skip the settle entirely.

        This phase is scroll-locked: nothing scrolls while the page settles.
        """
        if not self._settle_on_start or self._stop:
            return
        self._set(AgentState.OBSERVE_VIEWPORT)
        deadline = time.time() + max_wait
        last_sig = ""
        stable = 0
        while time.time() < deadline and not self._stop:
            self._force_rebuild = True
            analysis, _ = self._observe()
            if analysis is None or analysis.capture is None:
                break  # not a live capture target; nothing to settle
            sig = self._element_signature(analysis.scene)
            if sig and sig == last_sig:
                stable += 1
                if stable >= 2:
                    break
            else:
                stable = 0
            last_sig = sig
            time.sleep(poll)
        # Reset so the record-wait phase re-observes the settled screen fresh.
        self._cached_analysis = None
        self._last_signature = ""
        self._force_rebuild = False

    @staticmethod
    def _viewport(analysis: SceneAnalysis) -> tuple[int, int] | None:
        """The client-area viewport ``(width, height)``, or None when unknown.

        Vision targets only see the current viewport; the bbox of a below-fold
        field is outside it and must not be acted on until a scroll reveals it.
        """
        capture = analysis.capture
        if capture is None:
            return None
        width = getattr(capture, "width", 0) or 0
        height = getattr(capture, "height", 0) or 0
        if width <= 0 or height <= 0:
            return None
        return width, height

    @classmethod
    def _visible_fields(
        cls, fields: list[Any], viewport: tuple[int, int] | None
    ) -> list[Any]:
        """Fields whose band intersects the current viewport.

        Fields without a bbox (web-DOM controls) are always kept - visibility
        cannot be judged and skipping them would break pure-web targets. A
        field scrolled above the top edge (``bottom <= 0``) or fully below the
        fold (``top >= height``) is NOT visible yet.
        """
        if viewport is None:
            return fields
        _, height = viewport
        return [
            f
            for f in fields
            if f.bbox is None
            or (f.bbox.top < height and f.bbox.bottom > 0)
        ]

    @staticmethod
    def _is_submit_action(action: Action, submit_id: str | None) -> bool:
        return action.type == ActionType.SUBMIT or (
            action.type == ActionType.CLICK
            and submit_id is not None
            and action.field_id == submit_id
        )

    @staticmethod
    def _element_signature(scene: SceneDescription) -> str:
        """Order-independent snapshot of the scene geometry for change detection."""
        parts = []
        for e in scene.elements:
            if e.bbox is not None:
                parts.append(
                    f"{e.element_id}:{e.type.value}:{e.bbox.left},{e.bbox.top},{e.bbox.width},{e.bbox.height}"
                )
            else:
                parts.append(f"{e.element_id}:{e.type.value}:nobox")
        return "|".join(sorted(parts))

    def _expand_upload_section(
        self,
        scene: SceneDescription,
        submit_id: str | None = None,
        viewport: tuple[int, int] | None = None,
    ) -> bool:
        """Click a collapsed upload/attachment section header, if any.

        Expands the strongest candidate that is not the final submit button,
        is inside the current viewport, and has fields below it (i.e. it really
        is a section header, not a terminal action button). Returns True when a
        header was clicked so the caller re-observes.
        """
        for element in self._upload_section_candidates(scene, viewport, submit_id=submit_id):
            bbox = element.bbox
            if bbox is None:
                continue
            action = Action(
                type=ActionType.CLICK,
                field_id=element.element_id,
                bbox=bbox.shifted(*scene.screen_offset),
                confidence=element.confidence or 0.5,
                reason=f"expand '{element.label or element.name}' section",
            )
            result = self._executor.execute(action)
            if result.ok:
                self._expanded_sections.add(element.element_id)
                logger.info("expanded upload section '{}'", element.label or element.name)
                return True
        return False

    def _upload_section_candidates(
        self,
        scene: SceneDescription,
        viewport: tuple[int, int] | None,
        submit_id: str | None = None,
    ) -> list[ScreenElement]:
        """Expandable upload/attachment headers in the current viewport.

        Excludes the final submit/upload button and action-strip buttons (they
        are clicked once at the end, never expanded), and anything below the
        fold (the current viewport is handled before any scroll happens).
        """
        candidates: list[ScreenElement] = []
        for element in find_upload_sections(scene, exclude_ids=self._expanded_sections):
            if element.section == "actions":
                continue
            if submit_id is not None and element.element_id == submit_id:
                continue
            bbox = element.bbox
            if bbox is None:
                continue
            if viewport is not None and bbox.top >= viewport[1]:
                continue
            has_fields_below = any(
                f.bbox is not None and f.bbox.top >= bbox.bottom
                for f in discover_fields(scene)
            )
            if has_fields_below:
                candidates.append(element)
        return candidates

    def can_scroll(
        self,
        scene: SceneDescription,
        handled_ids: set[str],
        viewport: tuple[int, int] | None,
        results: list[ActionResult],
        submit_id: str | None = None,
    ) -> bool:
        """Scroll permission: True only when the current viewport is complete.

        Scrolling is the LAST operation for a viewport. It delegates to the
        :class:`ViewportModel` NO SCROLL RULE - every visible field handled,
        every dropdown/date done, uploads/attachments checked, and every value
        verified - before any scroll is permitted. The first blocked gate is
        surfaced in ``self._scroll_blocked_reason`` for the debug dump.
        """
        model = self._build_viewport_model(scene, handled_ids, viewport, results)
        ok = model.can_scroll
        self._scroll_blocked_reason = model.scroll_blocked_reason()
        self._write_viewport_debug(model)
        return ok

    def can_reveal_scroll(
        self,
        scene: SceneDescription,
        handled_ids: set[str],
        viewport: tuple[int, int] | None,
        results: list[ActionResult],
        submit_id: str | None = None,
    ) -> bool:
        """Reveal-pass scroll permission: like ``can_scroll`` but never blocked
        by a prior verification failure.

        The reveal pass exists to DISCOVER the fields still below the fold. A
        value that failed to verify on one field must not stop it from scrolling
        to reach the rest of the form (Issue 1) - otherwise a single miss freezes
        the scan and every lower field is skipped. Only unhandled VISIBLE
        controls (fields, dropdowns, dates, uploads) may block the scroll; every
        gate is exactly the ViewportModel NO SCROLL RULE minus the verification
        gate. Verification still runs per field; it just never halts discovery.
        """
        model = self._build_viewport_model(scene, handled_ids, viewport, results)
        ok = model.viewport_complete
        self._scroll_blocked_reason = None if ok else model.reveal_blocked_reason()
        self._write_viewport_debug(model)
        return ok

    def _build_viewport_model(
        self,
        scene: SceneDescription,
        handled_ids: set[str],
        viewport: tuple[int, int] | None,
        results: list[ActionResult],
    ) -> ViewportModel:
        return ViewportModel(
            scene=scene,
            viewport=viewport,
            handled_ids=set(handled_ids),
            expanded_upload_ids=self._expanded_sections,
            results=list(results),
            scroll_position=self._scroll_position,
        )

    def _lock_scrolling(self) -> None:
        """Freeze scrolling while the current viewport is observed/filled."""
        self._executor.set_scroll_allowed(lambda: False)

    def _unlock_scrolling(self) -> None:
        """Allow scroll-into-view again (reveal pass / final submit click)."""
        self._executor.set_scroll_allowed(lambda: True)

    def _panel_rects(self, scene: SceneDescription) -> dict[str, BBox | None]:
        """Absolute-screen regions of the two panels: left + right.

        Prefers the UIA field-map panel rects (left source list, right entry
        form), then any explicit region provider. When a provider yields two
        regions they are treated as [left, right]; a single region is treated
        as the entry form. The regions are later clipped to the visible client
        area so the cursor never lands below the fold.
        """
        rects: dict[str, BBox | None] = {PANEL_LEFT: None, PANEL_RIGHT: None}
        if self._field_map is not None:
            rects[PANEL_LEFT] = self._field_map.left_rect
            rects[PANEL_RIGHT] = self._field_map.right_rect
        if not any(rects.values()) and self._scroll_regions is not None:
            try:
                regions = [r for r in self._scroll_regions(scene) if r is not None]
            except Exception:
                regions = []
            if len(regions) >= 2:
                rects[PANEL_LEFT], rects[PANEL_RIGHT] = regions[0], regions[1]
            elif regions:
                rects[PANEL_RIGHT] = regions[0]
        return rects

    def _client_rect(self) -> tuple[int, int, int, int] | None:
        """Absolute client rect of the sandbox target, or None."""
        sandbox = getattr(self._executor, "_sandbox", None)
        if sandbox is None:
            return None
        try:
            target = sandbox.validate_target()
            if target is None or not target.client_rect:
                return None
            left, top, right, bottom = target.client_rect
            if right <= left or bottom <= top:
                return None
            return left, top, right, bottom
        except Exception:
            return None

    def _refresh_scroll_session(self) -> None:
        """Re-discover the UIA scroll containers for this observation round.

        The provider (wired by the desktop assistant) returns a fresh
        :class:`ScrollSession` whose containers carry the current scroll
        percent. No provider (tests / web / no UIA) means the reveal pass falls
        back to the click-focus wheel path.
        """
        if self._scroll_container_provider is None:
            self._scroll_session = None
            return
        try:
            session = self._scroll_container_provider()
        except Exception as exc:
            logger.debug("scroll container discovery failed: {}", exc)
            session = None
        self._scroll_session = session if (session is not None and session.available) else None

    def _scroll_container_map(self, scroll_ctrl: DualPanelScroll) -> dict[str, ScrollContainer | None]:
        """Match the discovered scroll containers onto the LEFT/RIGHT panels.

        Uses the field-map content rects (never hardcoded coordinates) so the
        source panel and entry panel each get their own scroll container.
        """
        result: dict[str, ScrollContainer | None] = {PANEL_LEFT: None, PANEL_RIGHT: None}
        if self._scroll_session is None:
            return result
        left_rect = self._field_map.left_rect if self._field_map is not None else None
        right_rect = self._field_map.right_rect if self._field_map is not None else None
        try:
            chosen = pick_left_right_containers(
                self._scroll_session.containers,
                left_rect,
                right_rect,
                self._client_rect(),
            )
        except Exception as exc:
            logger.debug("scroll container match failed: {}", exc)
            return result
        for name in (PANEL_LEFT, PANEL_RIGHT):
            result[name] = chosen.get(name)
        return result

    def _scroll_panels(self, analysis: SceneAnalysis, scroll_ctrl: DualPanelScroll) -> None:
        """Scroll BOTH panels one small incremental step, then settle.

        Called ONLY after ``can_reveal_scroll()`` has returned True (the
        current viewport is fully handled). Each panel is scrolled *directly*
        through its own discovered UIA scroll container (ScrollPattern, with
        verification + retry escalation) when available - the outer window and
        desktop are never scrolled. Without UIA containers the panel is located
        from the field map, click-focused INSIDE the panel and wheel-scrolled by
        the 250-350 px adaptive amount. If one panel moved while the other did
        not, the lagging panel is nudged again so the left source data and the
        right entry form always correspond to the same section. A human-paced
        settle (300-500 ms) lets the UI refresh before the next observation.
        """
        scene = analysis.scene
        index = getattr(analysis, "index", None)
        key = getattr(analysis, "record_key", None)
        self._snapshot("before-scroll", index or 0, key or "record")
        amount = scroll_ctrl.scroll_notches(scene)
        pixels = (scroll_ctrl.min_pixels + scroll_ctrl.max_pixels) // 2
        known = scroll_ctrl.known_panels()

        session = self._scroll_session
        if session is not None and session.scroller is not None:
            self._scroll_containers(session, scroll_ctrl, pixels, scene)
        elif not known:
            # No panel geometry: one human wheel scroll over the form.
            self._set(AgentState.SCROLLING, "SCROLLING FORM")
            self._executor.execute(Action(
                type=ActionType.SCROLL,
                value=None,
                scroll_amount=amount,
                reason="reveal next section of the form (dual-panel scroll unavailable)",
            ))
            self._scroll_position += amount * 50  # 1 wheel notch ~= 50px
        else:
            for name, panel in ((PANEL_LEFT, scroll_ctrl.left), (PANEL_RIGHT, scroll_ctrl.right)):
                if panel.rect is None or not scroll_ctrl.needs_scroll(name):
                    continue
                anchor = scroll_ctrl.scroll_anchor(name, scene)
                if anchor is None:
                    anchor = (panel.rect.center[0], panel.rect.top + 6)
                self._scroll_region(name, panel.rect, amount, anchor=anchor, reason=f"scroll {name} panel")
                panel.scroll_position += amount * 50
                self._scroll_position += amount * 50

            # Panel synchronization: if one side moved while the other did not
            # (and neither is at its bottom), nudge the lagging panel once more
            # before continuing so both sides stay in the same section.
            lagging = scroll_ctrl.lagging_panel()
            if lagging is not None:
                panel = scroll_ctrl.panel(lagging)
                if panel.rect is not None:
                    anchor = scroll_ctrl.scroll_anchor(lagging, scene)
                    if anchor is None:
                        anchor = (panel.rect.center[0], panel.rect.top + 6)
                    self._scroll_region(
                        lagging, panel.rect, amount, anchor=anchor, reason=f"resync {lagging} panel"
                    )
                    panel.scroll_position += amount * 50
                    self._scroll_position += amount * 50

        # Human-paced settle so the UI refreshes before the next observation.
        time.sleep(random.uniform(*scroll_ctrl.settle_range))
        self._snapshot("after-scroll", index or 0, key or "record")
        self._write_viewport_position(scene, scroll_ctrl)  # debug: current scroll position

    def _scroll_containers(
        self,
        session: ScrollSession,
        scroll_ctrl: DualPanelScroll,
        pixels: int,
        scene: SceneDescription,
    ) -> None:
        """Scroll every panel that still needs content via its UIA container.

        Each scroll is VERIFIED against that panel's own visible labels: a
        fresh observation is taken and the panel-scoped signature (element
        ids + types + visible labels + rects) must differ from the pre-scroll
        snapshot, otherwise the scroll failed and the :class:`PanelScroller`
        retries with the next method / a bigger distance. The lagging panel is
        re-scrolled so the two sides stay in the same section. A scroll that no
        longer moves anything updates ``more_content`` so the next round's
        bottom detection can finish.
        """
        before_signatures = {
            name: scroll_ctrl.panel_signature(scene, panel, include_labels=True)
            for name, panel in ((PANEL_LEFT, scroll_ctrl.left), (PANEL_RIGHT, scroll_ctrl.right))
            if panel.container is not None
        }

        def _verify(name: str) -> bool:
            self._set(AgentState.VERIFYING, "SCROLL VERIFY")
            refreshed = self.reobserve_scene()
            if refreshed is None:
                return False
            panel = scroll_ctrl.panel(name)
            before = before_signatures.get(name, "")
            return scroll_ctrl.panel_signature(refreshed, panel, include_labels=True) != before

        for name, panel in ((PANEL_LEFT, scroll_ctrl.left), (PANEL_RIGHT, scroll_ctrl.right)):
            container = panel.container
            if container is None or not scroll_ctrl.needs_scroll(name):
                continue
            self._scroll_one_container(session, panel, container, pixels, lambda name=name: _verify(name), name)
        lagging = scroll_ctrl.lagging_panel()
        if lagging is not None:
            panel = scroll_ctrl.panel(lagging)
            container = panel.container
            if container is not None:
                self._scroll_one_container(session, panel, container, pixels, lambda: _verify(lagging), lagging)

    def _scroll_one_container(
        self,
        session: ScrollSession,
        panel: Any,
        container: ScrollContainer,
        pixels: int,
        verify: Callable[[], bool],
        name: str,
    ) -> None:
        if session.scroller is None:
            return
        self._set(AgentState.SCROLLING, f"SCROLLING {name.upper()} PANEL")
        try:
            outcome = session.scroller.scroll_down(container, pixels, verify)
        except Exception as exc:
            logger.debug("container scroll failed for {} panel: {}", name, exc)
            outcome = None
        if outcome is not None:
            panel.scroll_position += pixels
            self._scroll_position += pixels
            panel.more_content = container.more_content
            method = "FAILED" if outcome.method == "none" else outcome.method.upper()
            self._set(AgentState.SCROLLING, f"SCROLLING {name.upper()} PANEL ({method})")
            logger.info(
                "scrolled {} panel via '{}' (changed={}, percent={})",
                name,
                outcome.method,
                outcome.changed,
                container.vertical_scroll_percent,
            )
        succeeded = outcome is not None and outcome.method != "none"
        if succeeded:
            self._panel_scroll_failures[name] = 0
            return
        failures = self._panel_scroll_failures.get(name, 0) + 1
        self._panel_scroll_failures[name] = failures
        if failures < _RAW_SCROLL_FAILSAFE_THRESHOLD or container.rect is None:
            return
        # Every structured method (UIA ScrollPattern, mouse wheel, scrollbar
        # drag, keyboard, plugin override) has now failed to move this panel
        # for `_RAW_SCROLL_FAILSAFE_THRESHOLD` cycles in a row. Rather than
        # let the panel go permanently idle (the exact "it will not scroll
        # and just sits there" failure mode), force the simplest possible
        # thing that can still work: click into the panel and send a big,
        # unconditional wheel scroll directly, bypassing PanelScroller
        # entirely. The counter is reset either way so this only fires again
        # after another full run of consecutive failures, not every cycle.
        logger.error(
            "{} panel scroll stuck after {} consecutive failures - forcing raw wheel fallback",
            name,
            failures,
        )
        self._set(AgentState.RECOVERY, f"SCROLLING {name.upper()} PANEL (FORCED FALLBACK)")
        anchor = (container.rect.center[0], container.rect.top + 6)
        self._scroll_region(
            name,
            container.rect,
            max(pixels * 3, self._scroll_max_pixels * 2),
            anchor=anchor,
            reason=f"forced raw wheel fallback for stuck {name} panel",
        )
        self._panel_scroll_failures[name] = 0

    def _scroll_region(
        self,
        name: str,
        region: BBox,
        amount: int,
        anchor: tuple[int, int] | None = None,
        reason: str = "scroll region",
    ) -> None:
        """Move the cursor inside the panel, CLICK to focus it, then wheel-scroll.

        A wheel event scrolls whichever pane sits under the cursor, so each
        panel of a split form is scrolled separately. The panel is click-focused
        FIRST (a human clicks inside the panel before scrolling) so the wheel
        is captured by THIS panel's own scrollbar - never the whole window.
        ``anchor`` is a safe point inside the panel (over a label / header), and
        the cursor is clamped to the visible client area so it never lands below
        the fold.
        """
        if anchor is None:
            cx = max(region.left + 4, min(region.right - 4, int(region.center[0])))
            cy = max(region.top + 4, min(region.bottom - 4, int(region.center[1])))
        else:
            cx = max(region.left + 4, min(region.right - 4, int(anchor[0])))
            cy = max(region.top + 4, min(region.bottom - 4, int(anchor[1])))
        self._set(AgentState.SCROLLING, f"SCROLLING {name.upper()} PANEL (MOUSE WHEEL)")
        try:
            self._mouse_move_to(cx, cy)
        except Exception as exc:
            logger.debug("move to scroll region failed: {}", exc)
        # Click INSIDE the panel to focus it so the wheel scrolls this panel.
        self._executor.execute(Action(
            type=ActionType.CLICK,
            value=None,
            field_id=None,
            bbox=BBox(cx - 1, cy - 1, 2, 2),
            reason=f"focus panel for scroll at ({cx},{cy})",
        ))
        self._executor.execute(Action(
            type=ActionType.SCROLL,
            value=None,
            scroll_amount=amount,
            reason=reason,
        ))

    def _mouse_move_to(self, x: int, y: int) -> None:
        """Best-effort human mouse move to absolute screen coords (if available)."""
        mouse = getattr(self._executor, "_mouse", None)
        if mouse is not None:
            mouse.move_to(x, y)

    # -- debug dumps ----------------------------------------------------------

    def _debug_path(self, name: str) -> Path | None:
        if self._debug_dir is None:
            return None
        path = self._debug_dir / name
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
        except Exception:
            return None
        return path

    def _write_debug(self, name: str, data: Any) -> None:
        path = self._debug_path(name)
        if path is None:
            return
        try:
            path.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        except Exception as exc:
            logger.debug("failed to write {}: {}", path, exc)

    def _dump_run_metrics(self, summary: WorkflowSummary) -> None:
        """Write per-field stage timings to ``debug/performance/run_metrics.json``.

        The spec's per-stage budget split (discovery/action/verify/ocr/recovery)
        is auditable here: each verifyable field's action/verify/recovery times
        accumulate, so a field eating 70-80s in retries is immediately visible.
        """
        if self._debug_dir is None:
            return
        try:
            metrics = self._executor.field_metrics() if self._executor is not None else {}
        except Exception as exc:
            logger.debug("field metrics unavailable: {}", exc)
            metrics = {}
        if not metrics:
            return
        folder = self._debug_dir / "performance"
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except Exception:
            return
        totals: dict[str, float] = {}
        for entry in metrics.values():
            for stage, seconds in entry.get("stages", {}).items():
                totals[stage] = totals.get(stage, 0.0) + float(seconds)
        try:
            (folder / "run_metrics.json").write_text(
                json.dumps({
                    "run": {
                        "completed": summary.completed,
                        "failed": summary.failed,
                        "unverified_records": summary.unverified,
                        "unverified_fields": summary.unverified_fields,
                        "stopped_reason": summary.stopped_reason,
                    },
                    "fields": metrics,
                    "totals_by_stage": totals,
                }, ensure_ascii=False, indent=2, default=str),
                encoding="utf-8",
            )
        except Exception as exc:
            logger.debug("failed to write run_metrics.json: {}", exc)

    def _dump_verification_debug(self, summary: WorkflowSummary) -> None:
        """Write every structured verification event for post-mortem analysis.

        Each event carries expected/observed, the strategy evidence and the
        window geometry at read time - so a "vision read empty" can be traced
        to a genuinely empty field vs. a stale bbox (window resized/scrolled
        between the write and the read).
        """
        if self._debug_dir is None:
            return
        events = self._executor.verification_events() if self._executor is not None else []
        if not events:
            return
        self._write_debug("verification_debug.json", {
            "run": {
                "completed": summary.completed,
                "failed": summary.failed,
                "unverified_records": summary.unverified,
                "unverified_fields": summary.unverified_fields,
                "stopped_reason": summary.stopped_reason,
            },
            "verifications": events,
            "summary": {
                "total": len(events),
                "matched": sum(1 for e in events if e.get("ok")),
                "mismatched": sum(1 for e in events if not e.get("ok")),
                "unknown": sum(1 for e in events if e.get("status") == "UNKNOWN"),
            },
        })

    def _write_viewport_debug(self, model: ViewportModel) -> None:
        """Emit the current viewport model the last time scrolling was weighed.

        Let an operator audit exactly what the agent saw and why it did or did
        not scroll (every NO SCROLL RULE gate, every pending field).
        """
        if self._debug_dir is None:
            return
        try:
            payload = model.to_dict()
            payload["scroll_blocked_reason"] = self._scroll_blocked_reason
            self._write_debug("viewport.json", payload)
        except Exception as exc:
            logger.debug("viewport debug write failed: {}", exc)

    def _write_viewport_position(self, scene: SceneDescription, scroll_ctrl: DualPanelScroll) -> None:
        """Persist the current scroll offset for both panels while scanning (debug)."""
        if self._debug_dir is None:
            return
        data = {
            "scroll_position": self._scroll_position,
            "window_title": scene.window_title,
            "layout_summary": scene.layout_summary,
            "panels": scroll_ctrl.to_dict().get("panels", {}),
            "upload_visible": scroll_ctrl.upload_visible,
        }
        self._write_debug("scroll_position.json", data)

    def _dump_record_debug(
        self, plan: FillPlan, results: list[ActionResult], index: int, record: SourceRecord
    ) -> None:
        if self._debug_dir is None:
            return
        self._write_session("record.json", {
            "index": index,
            "key": record.record_key,
            "title": record.title,
            "pairs": dict(record.pairs),
            "ordered_labels": record.ordered_labels,
        })
        self._write_debug("planner.json", plan.to_dict())
        self._write_debug("execution_plan.json", plan.to_dict())
        self._write_debug("execution.json", {
            "record_index": index,
            "actions": [r.to_dict() for r in results],
        })
        self._write_debug("verification.json", {
            "record_index": index,
            "results": [
                {
                    "field_id": r.action.field_id,
                    "action": r.action.type.value,
                    "expected": r.action.expected or r.action.value,
                    "verified": r.verified,
                    "success": r.success,
                    "evidence": r.verification_evidence or r.message,
                }
                for r in results
            ],
        })

    def _write_session(self, name: str, payload: dict) -> Path | None:
        if self._session_dir is None:
            return None
        try:
            self._session_dir.mkdir(parents=True, exist_ok=True)
            path = self._session_dir / name
            path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            return path
        except Exception as exc:
            logger.debug("session write failed: {}", exc)
            return None

    def _snapshot(self, context: str, index: int, key: str) -> Path | None:
        """Capture a screenshot for the given record lifecycle point.

        Writes ``debug/screenshots/{index}-{key}-{context}.png`` via the
        target-agnostic capture callback. Never raises.
        """
        if self._capture_callback is None or self._debug_dir is None:
            return None
        folder = self._debug_dir / "screenshots"
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except Exception:
            return None
        safe_key = "".join(c if c.isalnum() or c in "-_" else "_" for c in (key or "record"))
        path = folder / f"{index:04d}-{safe_key}-{context}.png"
        try:
            if self._capture_callback(path):
                log_screenshot(path, context)
                return path
        except Exception as exc:
            logger.debug("screenshot {} failed: {}", context, exc)
        return None

    def _dump_timeline(self, summary: WorkflowSummary) -> None:
        if self._session_dir is None:
            return
        self._write_session("timeline.json", {
            "records": summary.completed,
            "failed": summary.failed,
            "unverified_records": summary.unverified,
            "unverified_fields": summary.unverified_fields,
            "stopped_reason": summary.stopped_reason,
            "duration_ms": summary.total_duration * 1000.0,
            "records_json": [r.to_dict() for r in summary.records],
        })

    def _dump_failure(self, summary: WorkflowSummary) -> None:
        """Write ``failure.json`` when the run did not fully succeed.

        A clean ``max_records`` stop is not a failure; an aborted run (no
        record, stopped early with pending records) is.
        """
        if self._debug_dir is None:
            return
        failed = summary.failed
        clean_stop = not summary.stopped_reason or summary.stopped_reason.startswith("max_records")
        if failed == 0 and clean_stop:
            return
        payload = {
            "failed_records": failed,
            "completed_records": summary.completed,
            "unverified_records": summary.unverified,
            "unverified_fields": summary.unverified_fields,
            "stopped_reason": summary.stopped_reason,
            "duration_ms": summary.total_duration * 1000.0,
            "records": [r.to_dict() for r in summary.records],
            "state": self._states.state.value,
            "current_field": self._last_field,
            "planner_status": self._planner_status,
            "last_exception": self._last_exception,
            "no_record_reason": self._no_record_last_reason,
        }
        self._write_debug("failure.json", payload)

    def _dump_focus_history(self) -> None:
        """Write ``focus_history.json`` from the RECOVERY event stream.

        Every focus-related pause/refocus decision published by the sandbox or
        the workflow is replayed here so focus-loss episodes are auditable
        offline without re-running the automation.
        """
        if self._debug_dir is None:
            return
        try:
            history = [
                e.to_dict()
                for e in self._bus.history(EventType.RECOVERY)
                if "focus" in str(e.data.get("reason", "")).lower()
            ]
            self._write_debug("focus_history.json", {
                "count": len(history),
                "events": history,
            })
        except Exception as exc:
            logger.debug("focus_history write failed: {}", exc)

<<<<<<< HEAD
    def _dump_watchdog(self) -> None:
        """Write ``watchdog.json`` summarising both watchdog levels.

        Level 1 (sandbox focus/recovery events) and level 2 (state-budget
        overruns) are consolidated so a stuck run is diagnosable offline: which
        states overran, how often each was repeated, and what focus recovery
        happened around them.
        """
        if self._debug_dir is None:
            return
        try:
            recovery_events = [
                e.to_dict()
                for e in self._bus.history(EventType.RECOVERY)
            ]
            overruns = {
                str(state.value): count
                for state, count in self._state_overruns.items()
                if count > 0
            }
            self._write_debug("watchdog.json", {
                "level1_focus_events": [
                    e for e in recovery_events
                    if "focus" in str(e.get("data", {}).get("reason", "")).lower()
                ],
                "level2_state_overruns": overruns,
                "state_overrun_total": sum(overruns.values()),
                "recovery_events_total": len(recovery_events),
            })
        except Exception as exc:
            logger.debug("watchdog write failed: {}", exc)

=======
>>>>>>> 506caa78300fd5640f3fd0dcb51ac6f142dcd8ca
    # -- helpers --------------------------------------------------------------

    def _await_record(self, previous_key: str | None) -> tuple[SceneAnalysis, SourceRecord] | None:
        """Wait for the next source record.

        Event-driven: the screen model is only rebuilt when the observed scene
        actually changes (app switch, upload click, left-panel change, scroll or
        focused-control change). On ``records == 0`` the loop never terminates:
        it shows "No valid record detected.", keeps retrying and waits for the
        next record. It only returns ``None`` when the loop is stopped.
        """
        self._set(AgentState.WATCHING)
        while not self._stop:
            self._check_state_budget()
            if self._next_timeout is not None and not self._stop:
                deadline = time.time() + self._next_timeout
                while not self._stop and time.time() < deadline:
                    analysis, changed = self._observe()
                    if analysis is not None and changed:
                        record = self._extract_record(analysis.scene)
                        if record is not None and self._accept_record(record, previous_key, analysis.scene):
                            self._bus.publish(EventType.NEXT_RECORD_DETECTED, {"key": record.record_key})
                            return analysis, record
                        if record is None:
                            # A no-record (e.g. loading) screen must not be
                            # cached by signature: force a fresh observation on
                            # the next poll so the following record is detected.
                            self._force_rebuild = True
                            time.sleep(self._next_poll)
                            continue
                        if self._same_record(record, previous_key):
                            self._bus.publish(EventType.NEXT_RECORD_WAITING, {"key": record.record_key})
                    time.sleep(self._next_poll)
                if not self._stop:
                    self._report_no_record()
            else:
                analysis, changed = self._observe()
                if analysis is not None and changed:
                    record = self._extract_record(analysis.scene)
                    if record is not None and self._accept_record(record, previous_key, analysis.scene):
                        self._bus.publish(EventType.NEXT_RECORD_DETECTED, {"key": record.record_key})
                        return analysis, record
                    if record is None:
                        # Never let a no-record screen stay cached (see above).
                        self._force_rebuild = True
                time.sleep(self._next_poll)
        self._set(AgentState.STOPPED)
        return None

    def _accept_record(self, record: SourceRecord, previous_key: str | None, scene: SceneDescription) -> bool:
        key = record.record_key
        if key and key != previous_key:
            return True
        if not record.pairs:
            return False
        if key is None and scene.layout_summary != (self._last_layout or ""):
            self._last_layout = scene.layout_summary
            return True
        return False

    def _same_record(self, record: SourceRecord, previous_key: str | None) -> bool:
        key = record.record_key
        if key and key == previous_key:
            return True
        return bool(key is None and self._last_layout and record.record_key is None)

    def reobserve_scene(self) -> SceneDescription | None:
        """Force a fresh observation and return the (field-map-merged) scene.

        Used by the executor after scrolling so bboxes stay accurate. Never
        raises: returns None on observation failure.
        """
        try:
            self._force_rebuild = True
            analysis, _ = self._observe()
            return analysis.scene if analysis is not None else None
        except Exception as exc:
            logger.debug("reobserve_scene failed: {}", exc)
            return None

    def _observe(self) -> tuple[SceneAnalysis | None, bool]:
        """Observe the target and rebuild the screen model only when changed.

        Returns ``(analysis, changed)``. ``changed`` is True when the screen
        model was rebuilt (first observation, app change, scroll, focus change,
        upload click or forced rebuild).
        """
        self._set(AgentState.OBSERVING)
        signature = self._target.signature() if hasattr(self._target, "signature") else ""
        if self._cached_analysis is None or self._force_rebuild or signature != self._last_signature:
            self._last_signature = signature
            self._force_rebuild = False
            # Live desktop forms refresh their UIA geometry after a scroll (the
            # below-fold fields move into view). Re-merge the current rects so
            # the reveal pass keeps discovering newly visible fields instead of
            # working against stale attach-time geometry.
            if self._field_map_refresh is not None:
                try:
                    refreshed = self._field_map_refresh()
                    if refreshed is not None:
                        self._field_map = refreshed
                except Exception as exc:
                    logger.debug("field map refresh failed: {}", exc)
            analysis = self._target.observe()
            if analysis is None:
                return None, False
            if self._scene_hook is not None:
                try:
                    analysis.scene = self._scene_hook(analysis.scene)
                except Exception:
                    logger.exception("scene hook failed; using raw scene")
            self._merge_field_map(analysis.scene)
            self._bus.publish(EventType.OBSERVED, analysis.to_dict())
            self._cached_analysis = analysis
            self._write_debug("vision_output.json", {
                "provider": analysis.scene.provider,
                "window_title": analysis.scene.window_title,
                "layout_summary": analysis.scene.layout_summary,
                "screen_offset": list(analysis.scene.screen_offset),
                "sections": [s.to_dict() for s in analysis.scene.sections],
                "elements": [e.to_dict() for e in analysis.scene.elements],
            })
            return analysis, True
        return self._cached_analysis, False

    def _extract_record(self, scene: SceneDescription) -> SourceRecord | None:
        """Run the Record Extraction stage from UIA/OCR source pairs, falling
        back to the VLM scene reader. On failure writes ``debug/no_record.json``
        and ``debug/record_failure.json`` (Step 6).
        """
        self._set(AgentState.RECORD_EXTRACTION)
        pairs = self._collect_source_pairs(scene)
        result = self._record_builder.build(pairs, title=scene.window_title)
        if result.record is None:
            self._report_no_record(scene, result)
            self._write_record_failure(scene, result)
            return None
        self._bus.publish(EventType.SOURCE_READ, result.record.to_dict())
        return result.record

    def _write_record_failure(self, scene: SceneDescription, result: RecordBuildResult) -> None:
        """Write ``debug/record_failure.json`` with full diagnostics (Step 6)."""
        if self._debug_dir is None:
            return
        payload = {
            "reason": result.reason or "record could not be built",
            "detected_labels": list(result.labels),
            "detected_values": list(result.values),
            "missing_required": list(result.missing_required),
            "missing_controls": [],
            "missing_mappings": [],
            "window_title": scene.window_title,
            "layout_summary": scene.layout_summary,
        }
        # Report missing controls/mappings from the field map if available.
        if self._field_map is not None:
            payload["missing_controls"] = [
                n.name for n in (self._field_map.right_fields or [])
                if n.control_type in {"Edit", "ComboBox", "CheckBox", "RadioButton"}
            ]
            payload["missing_mappings"] = [
                m for m in (self._field_map.mappings or [])
            ]
        self._write_debug("record_failure.json", payload)

    @staticmethod
    def _clean_node_name(node: Any) -> str:
        return (node.name or node.automation_id or "").strip()

    def _collect_source_pairs(self, scene: SceneDescription) -> list[tuple[str, str]]:
        """Prefer exact UIA/OCR source pairs over the VLM scene pairs."""
        if self._field_map is not None and self._field_map.has_source:
            if self._ocr_callback is not None and self._field_map.left_rect is not None:
                left = self._field_map.left_rect
                try:
                    lines = self._ocr_callback(BBox(left.left, left.top, left.width, left.height))
                except Exception as exc:
                    logger.debug("source OCR failed: {}", exc)
                    lines = []
                self._write_debug("ocr_output.json", {
                    "region": {"left": left.left, "top": left.top, "width": left.width, "height": left.height},
                    "lines": [line.to_dict() for line in lines],
                })
                pairs = pair_source_pairs(lines, self._field_map.left_labels)
                if pairs:
                    return pairs
            labels = self._field_map.left_labels
            if labels:
                return [(self._clean_node_name(label), "") for label in labels]
        record = self._source_reader.read(scene)
        return [(label, record.pairs.get(label, "")) for label in record.ordered_labels]

    def _report_no_record(self, scene: SceneDescription | None = None, result: RecordBuildResult | None = None) -> None:
        """Surface a no-record condition and write ``debug/no_record.json``."""
        self._set(AgentState.WAITING)
        reason = (result.reason if result is not None else None) or "no valid record detected"
        if reason != self._no_record_last_reason:
            self._no_record_last_reason = reason
            logger.warning("no record: {}", reason)
            self._bus.publish(EventType.RECOVERY, {"reason": reason, "state": "record_extraction"})
        if self._debug_dir is not None:
            if result is not None and scene is not None:
                try:
                    self._record_builder.write_no_record(self._debug_dir / "no_record.json", result, scene=scene)
                except Exception as exc:
                    logger.debug("no_record write failed: {}", exc)
            else:
                self._write_debug("no_record.json", {"reason": reason})
        self._bus.publish(EventType.NO_RECORD, {"reason": reason})

    def _merge_field_map(self, scene: SceneDescription) -> None:
        """Synthesise exact UIA geometry onto the observed scene when a map exists.

        The UIA field map replaces the VLM's fuzzy editable fields with exact
        controls and injects OCR source pairs, so mapping/planning/execution use
        reliable geometry even when the VLM fails to identify the form.
        """
        if self._field_map is None or not self._field_map.has_form:
            return
        origin_x, origin_y = scene.screen_offset
        added: list[ScreenElement] = []
        seen_ids: set[str] = set()

        for node in self._field_map.right_fields:
            if node.rect is None:
                continue
            element_id = f"uia-{node.handle or node.automation_id or len(added)}"
            if element_id in seen_ids:
                continue
            seen_ids.add(element_id)
            box = BBox(
                node.rect.left - origin_x,
                node.rect.top - origin_y,
                node.rect.width,
                node.rect.height,
            )
            label = (node.name or node.automation_id or "").strip()
            added.append(ScreenElement(
                element_id=element_id,
                type=node.element_type,
                label=label,
                name=node.name or "",
                bbox=box,
                confidence=1.0,
                value=None,
                required=None,
                disabled=not node.enabled,
                section="form",
                options=list(node.options),
            ))

        if self._field_map.has_source and self._ocr_callback is not None and self._field_map.left_rect is not None:
            left = self._field_map.left_rect
            try:
                lines = self._ocr_callback(BBox(left.left, left.top, left.width, left.height))
            except Exception as exc:
                logger.debug("source OCR failed: {}", exc)
                lines = []
            for label, value in pair_source_pairs(lines, self._field_map.left_labels):
                element_id = f"uia-src-{len(seen_ids)}"
                seen_ids.add(element_id)
                added.append(ScreenElement(
                    element_id=element_id,
                    type=ElementType.LABEL,
                    label=label,
                    name=label,
                    bbox=None,
                    confidence=0.9,
                    value=value or None,
                    section="source",
                ))

        if self._field_map.upload_button is not None and self._field_map.upload_button.rect is not None:
            btn = self._field_map.upload_button
            box = BBox(btn.rect.left - origin_x, btn.rect.top - origin_y, btn.rect.width, btn.rect.height)
            element_id = f"uia-btn-{btn.handle or 'upload'}"
            if element_id not in seen_ids:
                seen_ids.add(element_id)
                added.append(ScreenElement(
                    element_id=element_id,
                    type=ElementType.BUTTON,
                    label=btn.name or "Upload",
                    name=btn.name or "",
                    bbox=box,
                    confidence=1.0,
                    section="actions",
                ))

        if not added:
            return
        kept = [e for e in scene.elements if not e.editable]
        merged: dict[str, ScreenElement] = {e.element_id: e for e in kept}
        for element in added:
            merged[element.element_id] = element
        scene.elements = list(merged.values())
        scene.layout_summary = scene.layout_summary or "uia-anchored"

    def _find_submit(self, scene: SceneDescription) -> str | None:
        submitish = (
            "upload", "submit", "save", "next", "ok", "apply", "continue",
            "done", "finish", "update", "register", "create", "add", "confirm",
        )
        # An expandable "Upload Details" section header must never be treated as
        # the final submit button: it is clicked by _expand_upload_section so its
        # hidden fields get revealed. Any upload/attachment header that is not the
        # action-strip submit (and any already-expanded region) is excluded here
        # the same way the section expander excludes them - otherwise "upload" is
        # the first submit token and the header is wrongly clicked as submit.
        excluded: set[str] = set(self._expanded_sections)
        for candidate in find_upload_sections(scene, exclude_ids=self._expanded_sections):
            if candidate.section == "actions":
                continue
            excluded.add(candidate.element_id)
        buttons = [
            e
            for e in scene.elements
            if (e.type.value in {"button", "submit"} or e.section == "actions")
            and e.element_id not in excluded
        ]
        for label_token in submitish:
            for e in buttons:
                if label_token in (e.label or "").lower() and e.bbox is not None:
                    return e.element_id
        for e in buttons:
            if e.bbox is not None:
                return e.element_id
        for label_token in submitish:
            for e in buttons:
                if label_token in (e.label or "").lower():
                    return e.element_id
        for e in buttons:
            return e.element_id
        return None

    def _all_ok(self, results: list[ActionResult]) -> bool:
        if not results:
            return False
        return all(r.ok for r in results)

    @staticmethod
    def _skipped_fields(results: list[ActionResult]) -> list[str]:
        skipped = []
        for r in results:
            if r.success is False and r.action.field_id:
                skipped.append(r.action.field_id)
        return skipped

    @staticmethod
    def _unverified_fields(results: list[ActionResult]) -> list[str]:
        """Fields written but confirmed only as UNKNOWN (never a verified pass).

        Deduplicated by field id (the post-submit standalone VERIFY action
        re-reads the same field, so it would otherwise repeat the id).
        """
        unverified = []
        seen: set[str] = set()
        for r in results:
            if (r.success and r.verification_status == "UNKNOWN" and r.action.field_id
                    and r.action.field_id not in seen):
                unverified.append(r.action.field_id)
                seen.add(r.action.field_id)
        return unverified

    @staticmethod
    def _unmapped_required(mapping: MappingResult) -> list[str]:
        return [f.label for f in mapping.unmatched_fields if f.element.required]

    def _learn_aliases(self, record: SourceRecord, mapping: MappingResult, results: list[ActionResult]) -> None:
        """Conservatively remember fuzzy mappings that verified successfully."""
        if not self._alias_learning or self._memory is None:
            return
        verified_ids = {r.action.field_id for r in results if r.ok}
        for m in mapping.mappings:
            if m.method in {"token", "containment", "fuzzy"} and m.confidence >= 0.9 and m.target_id in verified_ids:
                try:
                    self._memory.learn_alias(m.source_label, m.target_label)
                    self._mapper.aliases.learn(m.source_label, m.target_label)
                except Exception as exc:
                    logger.debug("alias learning skipped: {}", exc)

    def _set(self, state: AgentState, detail: str | None = None) -> None:
        try:
            self._states.transition(state)
        except Exception:
            try:
                self._states.force(state)
            except Exception:
                pass
        self._state_entered[state] = time.time()
        self._state_warned.discard(state)
<<<<<<< HEAD
        if not hasattr(self, "_state_overruns"):
            self._state_overruns = {}
        if not hasattr(self, "_last_overrun_log"):
            self._last_overrun_log = {}
        self._state_overruns[state] = 0
        self._last_overrun_log.pop(state, None)
=======
>>>>>>> 506caa78300fd5640f3fd0dcb51ac6f142dcd8ca
        self._bus.publish(
            EventType.STATE_CHANGED,
            {"state": self._states.state.value, "detail": detail},
        )

    # -- watchdog -------------------------------------------------------------

    @staticmethod
    def _normalize_budget(budget: float | dict[str, float] | None) -> dict[str, float]:
        if isinstance(budget, dict):
            return {k: float(v) for k, v in budget.items()}
        default = float(budget) if budget is not None else 10.0
        budgets = {state.value: default for state in AgentState}
        budgets[AgentState.WATCHING.value] = 60.0  # next-record timeout governs this
        budgets[AgentState.OBSERVING.value] = 45.0  # VLM analysis can be slow
        budgets[AgentState.THINKING.value] = 30.0
        budgets[AgentState.WAITING.value] = 60.0
        budgets[AgentState.WAITING_FOR_START_FIELD.value] = 0.0  # user-driven, never times out here
        return budgets

    def _check_state_budget(self) -> None:
<<<<<<< HEAD
        """Level-2 watchdog: surface a state that has overrun its budget.

        Level 1 is the sandbox watchdog (`ExecutionSandbox._watchdog_loop`):
        target aliveness + focus. Level 2 (here) guards the workflow state
        machine: when a state stays past its budget the overrun is counted and
        re-warned every ``_overrun_repeat_log_seconds`` while it persists, so a
        genuinely stuck state keeps surfacing instead of logging once and going
        silent. It never blocks.
        """
=======
        """Log + surface a state that has overrun its budget (never blocks)."""
>>>>>>> 506caa78300fd5640f3fd0dcb51ac6f142dcd8ca
        state = self._states.state
        budget = self._state_budget.get(state.value, 10.0)
        if budget <= 0:
            return
        entered = self._state_entered.get(state)
        if entered is None:
            return
        elapsed = time.time() - entered
<<<<<<< HEAD
        if elapsed <= budget:
            return
        if not hasattr(self, "_state_overruns"):
            self._state_overruns = {}
        if not hasattr(self, "_last_overrun_log"):
            self._last_overrun_log = {}
        if not hasattr(self, "_overrun_repeat_log_seconds"):
            self._overrun_repeat_log_seconds = 30.0
        overruns = self._state_overruns.get(state, 0) + 1
        self._state_overruns[state] = overruns
        now = time.time()
        last_log = self._last_overrun_log.get(state, 0.0)
        if state not in self._state_warned or (now - last_log) >= self._overrun_repeat_log_seconds:
            self._state_warned.add(state)
            self._last_overrun_log[state] = now
            reason = f"state '{state.value}' overrun ({elapsed:.1f}s > {budget:.0f}s budget, tick {overruns})"
            watchdog_logger.warning("watchdog: {}", reason)
            logger.warning("watchdog: {}", reason)
            self._bus.publish(EventType.RECOVERY, {
                "reason": reason,
                "state": state.value,
                "elapsed": round(elapsed, 1),
                "budget": budget,
                "overruns": overruns,
            })
=======
        if elapsed > budget and state not in self._state_warned:
            self._state_warned.add(state)
            reason = f"state '{state.value}' overrun ({elapsed:.1f}s > {budget:.0f}s budget)"
            logger.warning("watchdog: {}", reason)
            self._bus.publish(EventType.RECOVERY, {"reason": reason, "state": state.value})
>>>>>>> 506caa78300fd5640f3fd0dcb51ac6f142dcd8ca


__all__ = ["AgentLoop", "RecordResult", "WorkflowSummary"]
