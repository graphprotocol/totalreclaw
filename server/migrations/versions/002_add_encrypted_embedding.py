"""Add encrypted_embedding column to facts table.

Revision ID: 002
Revises: 001
Create Date: 2026-02-26

Stores XChaCha20-Poly1305 encrypted embedding vectors (hex-encoded).
The server never decrypts this column -- it just stores and returns
the opaque blob for client-side cosine similarity re-ranking.

Nullable for backward compatibility with v1 facts that have no embedding.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "002"
down_revision: Union[str, None] = "001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("facts", sa.Column("encrypted_embedding", sa.Text, nullable=True))


def downgrade() -> None:
    op.drop_column("facts", "encrypted_embedding")
