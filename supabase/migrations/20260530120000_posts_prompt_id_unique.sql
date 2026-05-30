-- Phase 4: dedupe responses per prompt.
--
-- A prompt receives at most one post; photobooth posts have prompt_id null so
-- the partial predicate is the right shape. This index doubles as the
-- idempotency guard for concurrent /posts/confirm calls referencing the same
-- prompt with different post_ids — the second insert collides on this index
-- instead of silently creating a duplicate response.

create unique index if not exists posts_prompt_id_unique
    on public.posts (prompt_id)
    where prompt_id is not null;
