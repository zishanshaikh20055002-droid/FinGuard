"""Authentication request and safe response schemas."""

from pydantic import BaseModel, ConfigDict, Field, field_validator


class Credentials(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=12, max_length=128)

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value: str) -> str:
        value = value.strip().casefold()
        if value.count("@") != 1 or not value.split("@")[0] or "." not in value.split("@")[1]:
            raise ValueError("Invalid email address")
        return value


class LoginRequest(Credentials):
    password: str = Field(min_length=1, max_length=128)
