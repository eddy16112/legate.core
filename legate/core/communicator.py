# Copyright 2021-2022 NVIDIA Corporation
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

from . import FutureMap, Rect, Point
from .launcher import TaskLauncher as Task

if TYPE_CHECKING:
    from .runtime import Runtime


class Communicator(ABC):
    def __init__(self, runtime: Runtime) -> None:
        self._runtime = runtime
        self._context = runtime.core_context

        self._handles: dict[int, FutureMap] = {}
        # From launch domains to communicator future maps transformed to N-D
        self._nd_handles: dict[Rect, FutureMap] = {}

    def _get_1d_handle(self, volume: int) -> FutureMap:
        if volume in self._handles:
            return self._handles[volume]
        comm = self._initialize(volume)
        self._handles[volume] = comm
        return comm

    def _transform_handle(
        self, comm: FutureMap, launch_domain: Rect
    ) -> FutureMap:
        if launch_domain in self._nd_handles:
            return self._nd_handles[launch_domain]
        comm = self._runtime.delinearize_future_map(comm, launch_domain)
        self._nd_handles[launch_domain] = comm
        return comm

    def get_handle(self, launch_domain: Rect) -> FutureMap:
        comm = self._get_1d_handle(launch_domain.get_volume())
        if launch_domain.dim > 1:
            comm = self._transform_handle(comm, launch_domain)
        return comm

    def destroy(self) -> None:
        for volume, handle in self._handles.items():
            self._finalize(volume, handle)

    @abstractmethod
    def _initialize(self, volume: int) -> FutureMap:
        ...

    @abstractmethod
    def _finalize(self, volume: int, handle: FutureMap) -> None:
        ...


class NCCLCommunicator(Communicator):
    def __init__(self, runtime: Runtime) -> None:
        super(NCCLCommunicator, self).__init__(runtime)
        library = runtime.core_library

        self._init_nccl_id = library.LEGATE_CORE_INIT_NCCL_ID_TASK_ID
        self._init_nccl = library.LEGATE_CORE_INIT_NCCL_TASK_ID
        self._finalize_nccl = library.LEGATE_CORE_FINALIZE_NCCL_TASK_ID
        self._tag = library.LEGATE_GPU_VARIANT

    def _initialize(self, volume: int) -> FutureMap:
        # This doesn't need to run on a GPU, but will use it anyway
        task = Task(
            self._context, self._init_nccl_id, tag=self._tag, side_effect=True
        )
        nccl_id = task.execute_single()

        task = Task(self._context, self._init_nccl, tag=self._tag)
        task.add_future(nccl_id)
        handle = task.execute(Rect([volume]))
        self._runtime.issue_execution_fence()
        return handle

    def _finalize(self, volume: int, handle: FutureMap) -> None:
        task = Task(self._context, self._finalize_nccl, tag=self._tag)
        task.add_future_map(handle)
        task.execute(Rect([volume]))


class CPUCommunicator(Communicator):
    def __init__(self, runtime):
        super(CPUCommunicator, self).__init__(runtime)
        library = runtime.core_library

        self._init_cpucoll_id = library.LEGATE_CORE_INIT_CPUCOLL_ID_TASK_ID
        self._init_cpucoll_mapping = library.LEGATE_CORE_INIT_CPUCOLL_MAPPING_TASK_ID
        self._init_cpucoll = library.LEGATE_CORE_INIT_CPUCOLL_TASK_ID
        self._finalize_cpucoll = library.LEGATE_CORE_FINALIZE_CPUCOLL_TASK_ID
        self._tag = library.LEGATE_CPU_VARIANT

    def _initialize(self, volume):
        task = Task(
            self._context, self._init_cpucoll_id, tag=self._tag, side_effect=True
        )
        cpucoll_id = task.execute_single()
        task = Task(self._context, self._init_cpucoll_mapping, tag=self._tag)
        mapping_table_fm = task.execute(Rect([volume]))
        task = Task(self._context, self._init_cpucoll, tag=self._tag)
        task.add_future(cpucoll_id)
        for i in range(volume):
            f = mapping_table_fm.get_future(Point([i]))
            task.add_future(f)
        handle = task.execute(Rect([volume]))
        self._runtime.issue_execution_fence()
        return handle

    def _finalize(self, volume, handle):
        task = Task(self._context, self._finalize_cpucoll, tag=self._tag)
        task.add_future_map(handle)
        task.execute(Rect([volume]))
