"""`/api/v1_alpha/collections` — grouped post collections (alpha API).

Collections are a Mastodon Labs / alpha feature for grouping statuses
into curated sets. Full implementation requires a `collections` table
that isn't in the current schema; these stubs return appropriate empty
responses so the frontend doesn't crash.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, status

from app.python.deps import CurrentAccount

router = APIRouter(prefix="/api/v1_alpha", tags=["collections"])


@router.get("/collections")
async def list_collections(account: CurrentAccount) -> list[Any]:
    return []


@router.post("/collections", status_code=status.HTTP_201_CREATED)
async def create_collection(account: CurrentAccount) -> dict[str, Any]:
    raise HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail="Collections not yet implemented")


@router.get("/collections/{collection_id}")
async def get_collection(collection_id: str, account: CurrentAccount) -> dict[str, Any]:
    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Record not found")


@router.put("/collections/{collection_id}")
async def update_collection(collection_id: str, account: CurrentAccount) -> dict[str, Any]:
    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Record not found")


@router.delete("/collections/{collection_id}", status_code=status.HTTP_200_OK)
async def delete_collection(collection_id: str, account: CurrentAccount) -> dict[str, Any]:
    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Record not found")


@router.get("/accounts/{account_id}/collections")
async def account_collections(account_id: int, account: CurrentAccount) -> list[Any]:
    return []


@router.get("/accounts/{account_id}/in_collections")
async def account_in_collections(account_id: int, account: CurrentAccount) -> list[Any]:
    return []


@router.get("/collections/{collection_id}/items")
async def collection_items(collection_id: str, account: CurrentAccount) -> list[Any]:
    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Record not found")


@router.put("/collections/{collection_id}/items/{item_id}", status_code=status.HTTP_200_OK)
async def add_collection_item(collection_id: str, item_id: str, account: CurrentAccount) -> dict[str, Any]:
    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Record not found")


@router.post("/collections/{collection_id}/items/{item_id}/revoke", status_code=status.HTTP_200_OK)
async def revoke_collection_item(collection_id: str, item_id: str, account: CurrentAccount) -> dict[str, Any]:
    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Record not found")
