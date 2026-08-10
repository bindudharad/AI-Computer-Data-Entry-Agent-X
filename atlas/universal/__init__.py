"""Universal target detection and attach-first management.

The universal subsystem turns ATLAS from an MPF-specific script into a smart
computer-use agent: it *discovers* the user's current computer environment,
*classifies* the target application, *attaches* to the existing target (never
launching a duplicate), and only launches when the target genuinely does not
exist AND ``AUTO_LAUNCH_TARGET=true``.
"""

from __future__ import annotations

from atlas.universal.attach import AttachDecision, AttachFirstError, AttachFirstManager
from atlas.universal.classifier import ApplicationClassifier
from atlas.universal.detector import RankingPreferences, UniversalTargetDetector, Win32Accessor, ProcessAccessor
from atlas.universal.learning import MethodLearner, MethodProfile
from atlas.universal.models import (
    AttachmentMode,
    BrowserHealthState,
    CandidateTarget,
    Capability,
    TargetEnvironment,
    TargetLock,
    TargetSession,
)
from atlas.universal.performance import UniversalPerformanceReport
from atlas.universal.restart_policy import RestartMode, RestartPolicy
from atlas.universal.smart_wait import SmartWait, WaitTimeout

__all__ = [
    "TargetEnvironment",
    "TargetSession",
    "TargetLock",
    "CandidateTarget",
    "BrowserHealthState",
    "AttachmentMode",
    "Capability",
    "RankingPreferences",
    "UniversalTargetDetector",
    "Win32Accessor",
    "ProcessAccessor",
    "ApplicationClassifier",
    "AttachFirstManager",
    "AttachDecision",
    "AttachFirstError",
    "RestartPolicy",
    "RestartMode",
    "SmartWait",
    "WaitTimeout",
    "MethodLearner",
    "MethodProfile",
    "UniversalPerformanceReport",
]
