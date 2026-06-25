# Copyright 2026 3LC Inc.
# SPDX-License-Identifier: Apache-2.0
"""Plugin-facing shared utilities (the SDK contract surface).

These helpers depend only on ``tlc`` + the standard library — never on the
host web stack (litestar/socketio) or process-global execution state (queues).
They are staged here so out-of-process (venv) plugin workers can import them
without pulling in the service. Modules that talk about ``tlc`` objects
(tables/runs/urls/models) are candidates to eventually graduate into ``tlc``
core; UI-string and job-channel helpers are hub-native and stay here.
"""
