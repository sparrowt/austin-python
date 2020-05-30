# This file is part of "austin-python" which is released under GPL.
#
# See file LICENCE or go to http://www.gnu.org/licenses/ for full license
# details.
#
# austin-python is a Python wrapper around Austin, the CPython frame stack
# sampler.
#
# Copyright (c) 2018-2020 Gabriele N. Tornetta <phoenix1987@gmail.com>.
# All rights reserved.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

import re
from typing import Any, Dict, List

from austin import AustinError
from dataclasses import dataclass, field

# ---- Custom types ----
ThreadName = str
ProcessId = int
MicroSeconds = int
KiloBytes = int


# ---- Exceptions ----


class InvalidFrame(AustinError):
    """Invalid frame.

    Thrown when attempting to parse a string that is supposed to represent a
    frame, but has the wrong structure.
    """

    pass


class InvalidSample(AustinError):
    """Invalid sample.

    Thrown when attempting to parse a string that is supposed to represent a
    sample, but has the wrong structure.
    """

    pass


# ---- Dataclasses ----


@dataclass(frozen=True)
class Metrics:
    """Austin metrics."""

    time: MicroSeconds = 0
    memory_alloc: KiloBytes = 0
    memory_dealloc: KiloBytes = 0

    def __add__(self, other: "Metrics") -> "Metrics":
        return Metrics(
            self.time + other.time,
            self.memory_alloc + other.memory_alloc,
            self.memory_dealloc + other.memory_dealloc,
        )

    def copy(self):
        return self + Metrics()


@dataclass(frozen=True)
class Frame:
    """Python frame."""

    function: str
    filename: str
    line: int = 0

    @staticmethod
    def parse(frame: str) -> "Frame":
        """Parse the given string as a frame.

        A string representing a frame has the structure

            [frame] := <function> (<module>:<line number>)

        This static method attempts to parse the given string in order to
        identify the parts of the frame and returns an instance of the
        :class`Frame` dataclass with the corresponding fields filled in.
        """
        if not frame:
            raise InvalidFrame(frame)

        function, _, rest = frame.partition(" (")
        try:
            module, rest = rest.rsplit(":", maxsplit=1)
            line_no = int(rest.rstrip(")"))
        except ValueError:
            raise InvalidFrame(frame)

        return Frame(function, module, line_no)

    def __str__(self):
        return f"{self.function} ({self.filename}:{self.line})"


@dataclass
class Sample:
    """Austin sample."""

    pid: ProcessId
    thread: ThreadName
    metrics: Metrics
    frames: List[Frame] = field(default_factory=list)

    _ALT_FORMAT_RE = re.compile(r"\);L([0-9]+)")

    @staticmethod
    def parse(sample: str) -> "Sample":
        """Parse the given string as a frame.

        A string representing a sample has the structure

            [Process <pid>;]?Thread <tid>[;[frame]]* [metric]*

        This static method attempts to parse the given string in order to
        identify the parts of the sample and returns an instance of the
        :class`Sample` dataclass with the corresponding fields filled in.
        """
        if not sample:
            raise InvalidSample(sample)

        pid = 0
        rest = sample
        if rest[0] == "P":
            process, _, rest = rest.partition(";")
            _, pid = process.split()

        try:
            thread_frames, *metrics = rest.rsplit(maxsplit=3)
            int(metrics[-3])
        except (ValueError, IndexError):
            # Time/memory metrics
            thread_frames, *metrics = rest.rsplit(maxsplit=1)

        thread, _, frames = thread_frames.partition(";")
        if thread[0] != "T":
            raise InvalidSample(sample)

        if frames:
            colon = frames.rfind(";")
            if colon and frames[colon + 1] == "L":
                frames = Sample._ALT_FORMAT_RE.sub(r":\1)", frames)

        try:
            return Sample(
                pid=int(pid),
                thread=thread,
                metrics=Metrics(*(int(metric) for metric in metrics)),
                frames=[Frame.parse(frame) for frame in frames.split(";")]
                if frames
                else [],
            )
        except (ValueError, InvalidFrame):
            raise InvalidSample(sample)


@dataclass
class HierarchicalStats:
    """Base dataclass for representing hierarchical statistics.

    The statistics of a frame stack can be thought of as a rooted tree. Hence
    the hierarchy is established by the parent/child relation between the
    nodes in this tree. An instance of this class represents a node, and a
    leaf is given by those instances with an empty ``children`` attribute.

    The ``label`` attribute is used for indexing reasons and therefore should
    be of a hashable type.

    This class overrides the default ``add`` operator so that one can perform
    operations like ``stats1 + stats2``. Note, however, that instances of this
    class are not assumed to be immutable and indeed this operation will modify
    and return ``stats1`` with the outcome of the addition.
    """

    label: Any
    own: Metrics = field(default_factory=Metrics)
    total: Metrics = field(default_factory=Metrics)
    children: Dict[Any, "HierarchicalStats"] = field(default_factory=dict)

    def __lshift__(self, other: "HierarchicalStats") -> "HierarchicalStats":
        if self.label != other.label:
            return self

        self.own += other.own
        self.total += other.total

        for frame, child in other.children.items():
            try:
                self.children[frame] << child
            except KeyError:
                self.children[frame] = child

        return self

    def get_child(self, label: Any) -> "HierarchicalStats":
        return self.children[label]


@dataclass
class FrameStats(HierarchicalStats):
    """Frame statistics."""

    label: Frame
    height: int = 0
    children: Dict[Frame, "FrameStats"] = field(default_factory=dict)


class ThreadStats(HierarchicalStats):
    """Thread statistics."""

    label: ThreadName
    children: Dict[Frame, FrameStats] = field(default_factory=dict)


@dataclass
class ProcessStats:
    """Process statistics."""

    pid: ProcessId
    threads: Dict[ThreadName, ThreadStats] = field(default_factory=dict)

    def get_thread(self, thread_name: ThreadName) -> ThreadStats:
        """Get thread statistics from this process by name.

        If the given thread name is not registered with this current process
        statistics, then ``None`` is returned.
        """
        return self.threads.get(thread_name)


@dataclass
class AustinStats:
    """Austin statistics.

    This class is used to collect all the statistics about own and total time
    and/or memory generated by a run of Austin. The :func`update` method is
    used to pass a new :class`Sample` so that the statistics can be updated
    accordingly.
    """

    child_pid: ProcessId = 0
    processes: Dict[ProcessId, ProcessStats] = field(default_factory=dict)

    def get_process(self, pid: ProcessId) -> ProcessStats:
        """Get process statistics for the given PID."""
        return self.processes[pid]

    def update(self, sample: Sample) -> None:
        """Update the statistics with a new sample.

        Normally, you would what to generate a new instance of :class`Sample`
        by using :func`Sample.parse` on a sample string passed by Austin to
        the sample callback.
        """
        pid = sample.pid or self.child_pid
        thread_stats = ThreadStats(sample.thread, total=sample.metrics)

        # Convert the list of frames into a nested FrameStats instance
        stats = thread_stats
        container = thread_stats.children
        for height, frame in enumerate(sample.frames):
            stats = FrameStats(label=frame, height=height, total=sample.metrics)
            container[frame] = stats
            container = stats.children
        stats.own = stats.total.copy()

        if pid not in self.processes:
            self.processes[pid] = ProcessStats(pid, {sample.thread: thread_stats})
            return self

        process = self.processes[pid]
        if sample.thread not in process.threads:
            process.threads[sample.thread] = thread_stats
            return self

        process.threads[sample.thread] << thread_stats

        return self