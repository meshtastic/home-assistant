from meshtastic.protobuf import mesh_pb2


class MeshtasticError(Exception):
    pass


class MeshInterfaceError(MeshtasticError):
    pass


class MeshInterfaceRequestError(MeshtasticError):
    pass


class MeshRoutingError(MeshtasticError):
    def __init__(self, error: mesh_pb2.Routing.Error) -> None:
        self._error = error
        super().__init__(f"Routing error: {error}")
