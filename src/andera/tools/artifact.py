"""Agent-facing artifact tools — typed put/get over an ArtifactStore."""

from __future__ import annotations

from pydantic import BaseModel, Field

from andera.contracts import ArtifactStore, ToolResult

from ._runner import invoke


class PutArgs(BaseModel):
    content: bytes
    name: str
    mime: str | None = None
    sample_id: str | None = None
    run_id: str | None = None

    model_config = {"arbitrary_types_allowed": True}


class GetArgs(BaseModel):
    sha256: str = Field(min_length=64, max_length=64)


class ArtifactTools:
    def __init__(self, store: ArtifactStore) -> None:
        self._store = store

    async def put(self, args: PutArgs) -> ToolResult:
        async def run():
            tags = {
                k: v
                for k, v in {"sample_id": args.sample_id, "run_id": args.run_id}.items()
                if v is not None
            }
            artifact = await self._store.put(
                args.content, args.name, args.mime, **tags
            )
            return {"artifact": artifact.model_dump(mode="json")}

        # do not dump bytes into the audit payload
        safe_args = {
            "name": args.name,
            "mime": args.mime,
            "size": len(args.content),
            "sample_id": args.sample_id,
            "run_id": args.run_id,
        }
        return await invoke("artifact.put", safe_args, run)

    async def get(self, args: GetArgs) -> ToolResult:
        async def run():
            data = await self._store.get(args.sha256)
            return {"sha256": args.sha256, "size": len(data)}

        return await invoke("artifact.get", args.model_dump(), run)
