"""arq workers — async background jobs replacing Sidekiq workers.

Worker queues mirror Mastodon's existing Sidekiq queue names (`default`,
`mailers`, `push`, `pull`, `scheduler`, `ingress`) so Rails and Python
can both produce/consume from the same Redis namespace during cutover.
"""
