import socket
from contextlib import closing


def find_free_port():
    """
    From: https://stackoverflow.com/a/45690594
    """

    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as socket_object:
        socket_object.bind(('', 0))
        socket_object.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        return socket_object.getsockname()[1]
