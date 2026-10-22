"""The same incoming configuration shape for device and browser managers."""
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ObjectStorageConfigRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    provider: Literal["tencent_cos", "s3_compatible", "minio"]
    endpoint: str = Field(min_length=8, max_length=512)
    bucket: str = Field(min_length=3, max_length=128)
    region: str = Field(min_length=2, max_length=64)
    secret_id: str = Field(min_length=8, max_length=256, repr=False)
    secret_key: str = Field(min_length=8, max_length=512, repr=False)
