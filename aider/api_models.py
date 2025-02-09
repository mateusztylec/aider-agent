from typing import Optional
from pydantic import BaseModel


class GitRepoConfig(BaseModel):
    repo_url: str
    branch: str
    token: Optional[str] = None


class InitRequest(BaseModel):
    pretty: bool = False


class Message(BaseModel):
    content: str


class InstructionRequest(BaseModel):
    instruction: str
