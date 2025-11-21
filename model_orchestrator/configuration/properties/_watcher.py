from typing import Literal, Union

from pydantic import Field

from ._base import BaseConfig as _BaseConfig


class YamlPollerWatcherConfig(_BaseConfig):
    type: Literal["yaml"] = "yaml"
    path: str = Field(..., description="Path to the YAML file")


class OnchainPollerWatcherConfig(_BaseConfig):
    type: Literal["onchain"] = "onchain"
    url: str = Field("http://localhost:3000", description="Endpoint URL for on-chain polling")


PollerWatcherConfig = Union[YamlPollerWatcherConfig, OnchainPollerWatcherConfig]


class WatcherConfig(_BaseConfig):
    poller: PollerWatcherConfig = Field(...)
    interval: int = Field(10, description="Polling interval in seconds")
