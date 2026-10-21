-- Atelier knowledge base. Run once in the Supabase SQL editor of a NEW project.
-- (Kept in db/, not supabase/, so Supabase's GitHub integration doesn't try to run it by itself.)

create extension if not exists vector with schema extensions;

-- What the assistant searches: one row per chunk of text.
-- 3072 dimensions matches Google's gemini-embedding-001. pgvector can't index that size, but a
-- client's knowledge is tens or hundreds of chunks, so an exact search is instant.
create table public.documents (
  id bigserial primary key,
  content text,
  metadata jsonb default '{}'::jsonb,
  embedding extensions.vector(3072),
  client text generated always as (metadata->>'client') stored,
  doc text generated always as (metadata->>'doc') stored
);
create index documents_client_doc_idx on public.documents (client, doc);

-- Called by n8n's Supabase Vector Store node. `filter` limits a search to one client.
create or replace function public.match_documents (
  query_embedding extensions.vector(3072),
  match_count int default null,
  filter jsonb default '{}'
)
returns table (id bigint, content text, metadata jsonb, similarity float)
language sql stable
set search_path = public, extensions
as $$
  select d.id, d.content, d.metadata,
         1 - (d.embedding <=> query_embedding) as similarity
  from public.documents d
  where d.metadata @> filter
  order by d.embedding <=> query_embedding
  limit match_count
$$;

-- The original text of each document, so the admin page can show and edit it.
create table public.kb_sources (
  client text not null,
  doc text not null,
  content text not null,
  chars integer generated always as (char_length(content)) stored,
  updated_at timestamptz not null default now(),
  primary key (client, doc)
);

-- What the admin page lists.
create view public.kb_overview with (security_invoker = true) as
select s.client, s.doc, s.chars, s.updated_at, coalesce(c.chunks, 0) as chunks
from public.kb_sources s
left join (
  select client, doc, count(*) as chunks
  from public.documents
  group by client, doc
) c using (client, doc);

-- Row security on with no policies: only the server (service key) can read or write.
alter table public.documents enable row level security;
alter table public.kb_sources enable row level security;
