"""The XCUITest lifecycle: simctl device prep then a resident runner on the Simulator, or the same
runner without simctl prep on a real device (BE-0019, real-device targeting BE-0238).

This module also isolates the `.xctestrun` packaging helpers (`_patch_xctestrun_env`) and their
`plistlib` / `tempfile` / `shlex` imports, which only XCUITest needs, out of the environment modules
every platform loads.
"""

from ._attempt_failure import _AttemptFailure as _AttemptFailure
from ._functions import _COLD_POLL_SECONDS as _COLD_POLL_SECONDS
from ._functions import _COLD_SPAWN_ATTEMPTS as _COLD_SPAWN_ATTEMPTS
from ._functions import _LAUNCH_TIMEOUT_MARKER as _LAUNCH_TIMEOUT_MARKER
from ._functions import _MAX_WARM_REUSES as _MAX_WARM_REUSES
from ._functions import _MAX_WARM_REUSES_ENV as _MAX_WARM_REUSES_ENV
from ._functions import _RECOVERY_TIMEOUT as _RECOVERY_TIMEOUT
from ._functions import _RECOVERY_TIMEOUT_ENV as _RECOVERY_TIMEOUT_ENV
from ._functions import _RESPAWN_TIMEOUT_ENV as _RESPAWN_TIMEOUT_ENV
from ._functions import _RUN_ENDED_MARKERS as _RUN_ENDED_MARKERS
from ._functions import _RUN_ENDED_OVERLAP as _RUN_ENDED_OVERLAP
from ._functions import _RUNNER_STARTUP_TIMEOUT as _RUNNER_STARTUP_TIMEOUT
from ._functions import _RUNNER_STARTUP_TIMEOUT_ENV as _RUNNER_STARTUP_TIMEOUT_ENV
from ._functions import _allocate_port as _allocate_port
from ._functions import _await_cold_runner as _await_cold_runner
from ._functions import _classify_runner as _classify_runner
from ._functions import _destination as _destination
from ._functions import _diagnostic_reports_dir as _diagnostic_reports_dir
from ._functions import _major as _major
from ._functions import _max_warm_reuses as _max_warm_reuses
from ._functions import _never_ended as _never_ended
from ._functions import _no_recovery as _no_recovery
from ._functions import _patch_xctestrun_env as _patch_xctestrun_env
from ._functions import _recovery_timeout as _recovery_timeout
from ._functions import _reported_pid as _reported_pid
from ._functions import _reports_since as _reports_since
from ._functions import _resolve_runner as _resolve_runner
from ._functions import _respawn_timeout as _respawn_timeout
from ._functions import _run_ended_probe as _run_ended_probe
from ._functions import _runner_host_bundle_ids as _runner_host_bundle_ids
from ._functions import _runner_startup_timeout as _runner_startup_timeout
from ._functions import _RunnerTier as _RunnerTier
from ._functions import _spawn_cold_with_retry as _spawn_cold_with_retry
from ._functions import _terminate_process_group as _terminate_process_group
from ._functions import _zorder_client as _zorder_client
from ._functions import (
    bundled_runner_staleness_note,
    bundled_runner_toolchain_note,
    bundled_runner_toolchain_warning,
    effective_device_type,
    runner_source,
)
from ._recovery import _Recovery as _Recovery
from ._shared import _logger as _logger
from ._spawned import _Spawned as _Spawned
from .xcuitest_environment import _DEFAULT_RUNNER_LOG_DIR as _DEFAULT_RUNNER_LOG_DIR
from .xcuitest_environment import _RESULT_BUNDLE_ENV as _RESULT_BUNDLE_ENV
from .xcuitest_environment import _RUNNER_LOG_ENV as _RUNNER_LOG_ENV
from .xcuitest_environment import _RUNNER_LOG_TAIL_LINES as _RUNNER_LOG_TAIL_LINES
from .xcuitest_environment import _WARM_HEALTH_TIMEOUT as _WARM_HEALTH_TIMEOUT
from .xcuitest_environment import XcuitestEnvironment

__all__ = [
    "XcuitestEnvironment",
    "bundled_runner_staleness_note",
    "bundled_runner_toolchain_note",
    "bundled_runner_toolchain_warning",
    "effective_device_type",
    "runner_source",
]
