from custom_components.meshtastic.aiomeshtastic.errors import MeshtasticError


class ClientApiConnectionError(MeshtasticError):
    pass


class ClientApiConnectionInterruptedError(ClientApiConnectionError):
    def __init__(self, message: str | None = None) -> None:
        super().__init__(message or "Connection interrupted")


class ClientApiListenInterruptedError(ClientApiConnectionInterruptedError):
    pass


class ClientApiNotConnectedError(ClientApiConnectionError):
    def __init__(self) -> None:
        super().__init__("Not connected")


class ClientApiConnectFailedError(ClientApiConnectionError):
    def __init__(self) -> None:
        super().__init__("Connect failed")


class ClientApiDisconnectFailedError(ClientApiConnectionError):
    def __init__(self) -> None:
        super().__init__("Disconnect failed")
