from pydantic import BaseModel, ConfigDict


class BaseConfig(BaseModel):
    model_config = ConfigDict(
        extra='forbid',
        alias_generator=lambda string: string.replace('_', '-'),
    )
