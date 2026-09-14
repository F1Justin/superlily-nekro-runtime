from pydantic import BaseModel, ConfigDict, Field


class RPCRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    method: str = Field(..., min_length=1, max_length=128, pattern=r"^[A-Za-z_][A-Za-z0-9_]*$", description="远程调用的方法名")
    args: list = Field(default_factory=list, description="远程调用方法的参数")
    kwargs: dict = Field(default_factory=dict, description="远程调用方法的关键字参数")
