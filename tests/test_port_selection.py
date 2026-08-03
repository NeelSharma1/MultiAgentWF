import main


def test_bind_available_port_skips_a_port_in_use(monkeypatch):
    blocked_port = 8000

    class FakeSocket:
        def __init__(self, *_args):
            self.port = None

        def setsockopt(self, *_args):
            pass

        def bind(self, address):
            if address[1] == blocked_port:
                raise OSError("address already in use")
            self.port = address[1]

        def getsockname(self):
            return ("127.0.0.1", self.port)

        def close(self):
            pass

    monkeypatch.setattr(main.socket, "socket", FakeSocket)
    listener, selected_port = main.bind_available_port("127.0.0.1", blocked_port)

    assert selected_port == blocked_port + 1
    assert listener.getsockname()[1] == selected_port


def test_bind_available_port_rejects_invalid_start_port():
    for value in (0, 65536):
        try:
            main.bind_available_port(start_port=value)
        except ValueError:
            pass
        else:
            raise AssertionError("invalid start port was accepted")
