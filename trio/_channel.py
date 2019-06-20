from collections import deque, OrderedDict
from math import inf

import attr
from outcome import Error, Value

from .abc import SendChannel, ReceiveChannel, Channel
from ._util import generic_function, NoPublicConstructor

import trio
from ._core import enable_ki_protection


@generic_function
def open_memory_channel(max_buffer_size):
    """Open a channel for passing objects between tasks within a process.

    Memory channels are lightweight, cheap to allocate, and entirely
    in-memory. They don't involve any operating-system resources, or any kind
    of serialization. They just pass Python objects directly between tasks
    (with a possible stop in an internal buffer along the way).

    Channel objects can be closed by calling `~trio.abc.AsyncResource.aclose`
    or using ``async with``. They are *not* automatically closed when garbage
    collected. Closing memory channels isn't mandatory, but it is generally a
    good idea, because it helps avoid situations where tasks get stuck waiting
    on a channel when there's no-one on the other side. See
    :ref:`channel-shutdown` for details.

    Args:
      max_buffer_size (int or math.inf): The maximum number of items that can
        be buffered in the channel before :meth:`~trio.abc.SendChannel.send`
        blocks. Choosing a sensible value here is important to ensure that
        backpressure is communicated promptly and avoid unnecessary latency;
        see :ref:`channel-buffering` for more details. If in doubt, use 0.

    Returns:
      A pair ``(send_channel, receive_channel)``. If you have
      trouble remembering which order these go in, remember: data
      flows from left → right.

    In addition to the standard channel methods, all memory channel objects
    provide a ``statistics()`` method, which returns an object with the
    following fields:

    * ``current_buffer_used``: The number of items currently stored in the
      channel buffer.
    * ``max_buffer_size``: The maximum number of items allowed in the buffer,
      as passed to :func:`open_memory_channel`.
    * ``open_send_channels``: The number of open
      :class:`MemorySendChannel` endpoints pointing to this channel.
      Initially 1, but can be increased by
      :meth:`MemorySendChannel.clone`.
    * ``open_receive_channels``: Likewise, but for open
      :class:`MemoryReceiveChannel` endpoints.
    * ``tasks_waiting_send``: The number of tasks blocked in ``send`` on this
      channel (summing over all clones).
    * ``tasks_waiting_receive``: The number of tasks blocked in ``receive`` on
      this channel (summing over all clones).

    """
    if max_buffer_size != inf and not isinstance(max_buffer_size, int):
        raise TypeError("max_buffer_size must be an integer or math.inf")
    if max_buffer_size < 0:
        raise ValueError("max_buffer_size must be >= 0")
    state = MemoryChannelState(max_buffer_size)
    return (
        MemorySendChannel._create(state), MemoryReceiveChannel._create(state)
    )


@attr.s(frozen=True)
class MemoryChannelStats:
    current_buffer_used = attr.ib()
    max_buffer_size = attr.ib()
    open_send_channels = attr.ib()
    open_receive_channels = attr.ib()
    tasks_waiting_send = attr.ib()
    tasks_waiting_receive = attr.ib()


@attr.s
class MemoryChannelState:
    max_buffer_size = attr.ib()
    data = attr.ib(factory=deque)
    # Counts of open endpoints using this state
    open_send_channels = attr.ib(default=0)
    open_receive_channels = attr.ib(default=0)
    # {task: value}
    send_tasks = attr.ib(factory=OrderedDict)
    # {task: None}
    receive_tasks = attr.ib(factory=OrderedDict)

    def statistics(self):
        return MemoryChannelStats(
            current_buffer_used=len(self.data),
            max_buffer_size=self.max_buffer_size,
            open_send_channels=self.open_send_channels,
            open_receive_channels=self.open_receive_channels,
            tasks_waiting_send=len(self.send_tasks),
            tasks_waiting_receive=len(self.receive_tasks),
        )


@attr.s(cmp=False, repr=False)
class MemorySendChannel(SendChannel, metaclass=NoPublicConstructor):
    _state = attr.ib()
    _closed = attr.ib(default=False)
    # This is just the tasks waiting on *this* object. As compared to
    # self._state.send_tasks, which includes tasks from this object and
    # all clones.
    _tasks = attr.ib(factory=set)

    def __attrs_post_init__(self):
        self._state.open_send_channels += 1

    def __repr__(self):
        return (
            "<send channel at {:#x}, using buffer at {:#x}>".format(
                id(self), id(self._state)
            )
        )

    def statistics(self):
        # XX should we also report statistics specific to this object?
        return self._state.statistics()

    @enable_ki_protection
    def send_nowait(self, value):
        """Like `~trio.abc.SendChannel.send`, but if the channel's buffer is
        full, raises `WouldBlock` instead of blocking.

        """
        if self._closed:
            raise trio.ClosedResourceError
        if self._state.open_receive_channels == 0:
            raise trio.BrokenResourceError
        if self._state.receive_tasks:
            assert not self._state.data
            task, _ = self._state.receive_tasks.popitem(last=False)
            task.custom_sleep_data._tasks.remove(task)
            trio.hazmat.reschedule(task, Value(value))
        elif len(self._state.data) < self._state.max_buffer_size:
            self._state.data.append(value)
        else:
            raise trio.WouldBlock

    @enable_ki_protection
    async def send(self, value):
        """See `~trio.abc.SendChannel.send`.

        """
        await trio.hazmat.checkpoint_if_cancelled()
        try:
            self.send_nowait(value)
        except trio.WouldBlock:
            pass
        else:
            await trio.hazmat.cancel_shielded_checkpoint()
            return

        task = trio.hazmat.current_task()
        self._tasks.add(task)
        self._state.send_tasks[task] = value
        task.custom_sleep_data = self

        def abort_fn(_):
            self._tasks.remove(task)
            del self._state.send_tasks[task]
            return trio.hazmat.Abort.SUCCEEDED

        await trio.hazmat.wait_task_rescheduled(abort_fn)

    @enable_ki_protection
    def clone(self):
        """Clone this send channel object.

        This returns a new `MemorySendChannel` object, which acts as a
        duplicate of the original: sending on the new object does exactly the
        same thing as sending on the old object. (If you're familiar with
        `os.dup`, then this is a similar idea.)

        However, closing one of the objects does not close the other, and
        receivers don't get `EndOfChannel` until *all* clones have been
        closed.

        This is useful for communication patterns that involve multiple
        producers all sending objects to the same destination. If you give
        each producer its own clone of the `MemorySendChannel`, and then make
        sure to close each `MemorySendChannel` when it's finished, receivers
        will automatically get notified when all producers are finished. See
        :ref:`channel-mpmc` for examples.

        Raises:
          trio.ClosedResourceError: if you already closed this
              `MemorySendChannel` object.

        """
        if self._closed:
            raise trio.ClosedResourceError
        return MemorySendChannel._create(self._state)

    @enable_ki_protection
    async def aclose(self):
        if self._closed:
            await trio.hazmat.checkpoint()
            return
        self._closed = True
        for task in self._tasks:
            trio.hazmat.reschedule(task, Error(trio.ClosedResourceError()))
            del self._state.send_tasks[task]
        self._tasks.clear()
        self._state.open_send_channels -= 1
        if self._state.open_send_channels == 0:
            assert not self._state.send_tasks
            for task in self._state.receive_tasks:
                task.custom_sleep_data._tasks.remove(task)
                trio.hazmat.reschedule(task, Error(trio.EndOfChannel()))
            self._state.receive_tasks.clear()
        await trio.hazmat.checkpoint()


@attr.s(cmp=False, repr=False)
class MemoryReceiveChannel(ReceiveChannel, metaclass=NoPublicConstructor):
    _state = attr.ib()
    _closed = attr.ib(default=False)
    _tasks = attr.ib(factory=set)

    def __attrs_post_init__(self):
        self._state.open_receive_channels += 1

    def statistics(self):
        return self._state.statistics()

    def __repr__(self):
        return "<receive channel at {:#x}, using buffer at {:#x}>".format(
            id(self), id(self._state)
        )

    @enable_ki_protection
    def receive_nowait(self):
        """Like `~trio.abc.ReceiveChannel.receive`, but if there's nothing
        ready to receive, raises `WouldBlock` instead of blocking.

        """
        if self._closed:
            raise trio.ClosedResourceError
        if self._state.send_tasks:
            task, value = self._state.send_tasks.popitem(last=False)
            task.custom_sleep_data._tasks.remove(task)
            trio.hazmat.reschedule(task)
            self._state.data.append(value)
            # Fall through
        if self._state.data:
            return self._state.data.popleft()
        if not self._state.open_send_channels:
            raise trio.EndOfChannel
        raise trio.WouldBlock

    @enable_ki_protection
    async def receive(self):
        """See `~trio.abc.ReceiveChannel.receive`.

        """
        await trio.hazmat.checkpoint_if_cancelled()
        try:
            value = self.receive_nowait()
        except trio.WouldBlock:
            pass
        else:
            await trio.hazmat.cancel_shielded_checkpoint()
            return value

        task = trio.hazmat.current_task()
        self._tasks.add(task)
        self._state.receive_tasks[task] = None
        task.custom_sleep_data = self

        def abort_fn(_):
            self._tasks.remove(task)
            del self._state.receive_tasks[task]
            return trio.hazmat.Abort.SUCCEEDED

        return await trio.hazmat.wait_task_rescheduled(abort_fn)

    @enable_ki_protection
    def clone(self):
        """Clone this receive channel object.

        This returns a new `MemoryReceiveChannel` object, which acts as a
        duplicate of the original: receiving on the new object does exactly
        the same thing as receiving on the old object.

        However, closing one of the objects does not close the other, and the
        underlying channel is not closed until all clones are closed. (If
        you're familiar with `os.dup`, then this is a similar idea.)

        This is useful for communication patterns that involve multiple
        consumers all receiving objects from the same underlying channel. See
        :ref:`channel-mpmc` for examples.

        .. warning:: The clones all share the same underlying channel.
           Whenever a clone :meth:`receive`\\s a value, it is removed from the
           channel and the other clones do *not* receive that value. If you
           want to send multiple copies of the same stream of values to
           multiple destinations, like :func:`itertools.tee`, then you need to
           find some other solution; this method does *not* do that.

        Raises:
          trio.ClosedResourceError: if you already closed this
              `MemoryReceiveChannel` object.

        """
        if self._closed:
            raise trio.ClosedResourceError
        return MemoryReceiveChannel._create(self._state)

    @enable_ki_protection
    async def aclose(self):
        if self._closed:
            await trio.hazmat.checkpoint()
            return
        self._closed = True
        for task in self._tasks:
            trio.hazmat.reschedule(task, Error(trio.ClosedResourceError()))
            del self._state.receive_tasks[task]
        self._tasks.clear()
        self._state.open_receive_channels -= 1
        if self._state.open_receive_channels == 0:
            assert not self._state.receive_tasks
            for task in self._state.send_tasks:
                task.custom_sleep_data._tasks.remove(task)
                trio.hazmat.reschedule(task, Error(trio.BrokenResourceError()))
            self._state.send_tasks.clear()
            self._state.data.clear()
        await trio.hazmat.checkpoint()
