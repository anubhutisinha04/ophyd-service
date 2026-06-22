import threading
import time as ttime

import pytest

from bluesky_queueserver.manager.output_streaming import ReceiveSystemInfo, _default_zmq_info_topic

from .common import re_manager_cmd  # noqa: F401
from .common import (
    condition_environment_closed,
    condition_environment_created,
    use_zmq_encoding_for_tests,
    wait_for_condition,
    zmq_request,
)

timeout_env_open = 10


class ReceiveMessages(threading.Thread):
    def __init__(self, *, receiver_class, zmq_subscribe_addr, zmq_topic, encoding, timeout=0.1):
        super().__init__()
        self._rco = receiver_class(zmq_subscribe_addr=zmq_subscribe_addr, zmq_topic=zmq_topic, encoding=encoding)
        self._exit = False
        self.received_msgs = []
        self._timeout = timeout
        self.n_timeouts = 0

    def run(self):
        while True:
            try:
                _ = {} if (self._timeout is None) else {"timeout": self._timeout}
                msg = self._rco.recv(**_)
                self.received_msgs.append(msg)
            except TimeoutError:
                self.n_timeouts += 1
            if self._exit:
                break

    def stop(self):
        self._exit = True

    def subscribe(self):
        self._rco.subscribe()

    def unsubscribe(self):
        self._rco.unsubscribe()


# fmt: off
@pytest.mark.parametrize("stream_enabled", [True, False, None])
# fmt: on
def test_zmq_info_streaming_1(monkeypatch, re_manager_cmd, stream_enabled):  # noqa: F811
    """
    Test 0MQ streaming functionality: streaming of status messages.
    Test periodic streaming (once per second).
    Test that streamed status reflect current state of RE Manager.
    Test that streaming can be disabled.
    """
    address_info_server = "tcp://*:60621"
    address_info_client = "tcp://localhost:60621"

    params_server = [f"--zmq-info-addr={address_info_server}"]
    if stream_enabled is not None:
        params_server.append(f"--zmq-publish-console={'ON' if stream_enabled else 'OFF'}")

    zmq_encoding = use_zmq_encoding_for_tests()

    rm_info = ReceiveMessages(
        receiver_class=ReceiveSystemInfo,
        zmq_subscribe_addr=address_info_client,
        zmq_topic=_default_zmq_info_topic,
        encoding=zmq_encoding,
    )

    rm_info.start()

    re_manager_cmd(params_server)

    # Test periodic streaming of status messages (once per second)
    if stream_enabled is True:
        ttime.sleep(6)
        assert len(rm_info.received_msgs) > 5

        msg_prev = rm_info.received_msgs[-2]
        msg_last = rm_info.received_msgs[-1]
        assert msg_last["time"] > msg_prev["time"]
        status_prev = msg_prev["msg"]["status"]
        status_last = msg_last["msg"]["status"]
        assert status_last["status_uid"] == status_prev["status_uid"]
        assert status_last["manager_state"] == "idle"
        assert status_last["worker_environment_exists"] is False

    zmq_request("environment_open")
    assert wait_for_condition(time=timeout_env_open, condition=condition_environment_created)

    # The assumption is that only 'status' messages are streamed.
    # If other messages are streamed, then the test needs to be adjusted.
    if stream_enabled is True:
        status_1 = rm_info.received_msgs[-1]["msg"]["status"]
        assert status_1["worker_environment_exists"] is True

    zmq_request("environment_close")
    assert wait_for_condition(time=3, condition=condition_environment_closed)

    if stream_enabled is True:
        status_2 = rm_info.received_msgs[-1]["msg"]["status"]
        assert status_2["worker_environment_exists"] is False
        assert status_2["status_uid"] != status_1["status_uid"]

    if stream_enabled is not True:
        assert len(rm_info.received_msgs) == 0

    rm_info.stop()
    rm_info.join()
