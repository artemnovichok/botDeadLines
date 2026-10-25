import json
from uuid import UUID

from cryptography.fernet import Fernet, InvalidToken


class SessionCipher:
    def __init__(self, key: str) -> None:
        self.fernet = Fernet(key.encode())

    def encrypt(self, user_id: UUID, session: str) -> str:
        payload = json.dumps({"user_id": str(user_id), "session": session})
        return self.fernet.encrypt(payload.encode()).decode()

    def decrypt(self, user_id: UUID, encrypted: str) -> str:
        data = json.loads(self.fernet.decrypt(encrypted.encode()))
        if data["user_id"] != str(user_id):
            raise InvalidToken("Сессия принадлежит другому пользователю")
        return str(data["session"])
