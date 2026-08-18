"""Transport-neutral application errors shared by HTTP, agent, and MCP paths."""


class DomainError(RuntimeError):
    """Base error whose message is safe to return to an authenticated user."""


class InvalidCommand(DomainError):
    pass


class NotFound(DomainError):
    pass


class Conflict(DomainError):
    pass


class PreviewDrift(Conflict):
    def __init__(self, preview: dict) -> None:
        super().__init__("The preview changed; review the refreshed change-set")
        self.preview = preview


class HardDeleteAcknowledgementRequired(InvalidCommand):
    pass


class QuotaExceeded(DomainError):
    pass


class AgentDisabled(NotFound):
    pass
