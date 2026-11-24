"""Public errors contain stable codes, never database or credential details."""


class ServiceError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(code)
        self.status = status
        self.code = code
        self.message = message


def not_found() -> ServiceError:
    return ServiceError(404, "not_found", "Resource not found.")


def invalid(message: str = "Invalid operation.") -> ServiceError:
    return ServiceError(400, "invalid_operation", message)
