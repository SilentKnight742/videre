# 2. Server-Sent Events, not WebSocket

Status: accepted

## Context

The UI needs a live, ordered stream of graph changes. Traffic is one way: commands go over ordinary POST requests.

## Decision

Stream events over SSE at `GET /api/runs/{id}/stream`, resumable with `Last-Event-ID` or `?after=seq`.

## Consequences

Plain HTTP, automatic reconnect in the browser, and resume falls out of the event `seq`. No client-to-server messages on the stream, which we do not need.
