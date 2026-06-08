# ADR-0001: Material Forum and Admin UI Rewrite

Date: 2026-06-06

## Status

Accepted for implementation.

## Context

The material forum currently has two UI surfaces:

- The desktop client embeds `_mext` as a PyQt/qfluentwidgets plugin.
- The server repository at `D:\Project_Folder\server` exposes FastAPI routes and
  contains `admin-ui`, a Vite/Vue/Naive UI management console.

The server repository does not contain a NiceGUI application. Searches for
`nicegui`, `from nicegui`, and `ui.page` found no active NiceGUI code. Therefore
the web rewrite target is the existing Vue admin console, not a NiceGUI app.

## Framework Evidence

shadcn/ui is a React/TSX component source workflow, not a Python UI runtime. The
official shadcn/ui docs describe using the CLI to initialize a project and add
components, then importing components such as `Button` from
`@/components/ui/button` and rendering them in TSX. Relevant sources:

- https://ui.shadcn.com/docs
- https://ui.shadcn.com/docs/cli
- https://ui.shadcn.com/docs/components/button
- https://ui.shadcn.com/docs/components/tabs

React and Next.js establish the runtime model needed by shadcn/ui. React
documents components, JSX, DOM roots, and state updates via hooks; Next.js
documents Client Components and the `'use client'` directive for interactive UI
that uses state, event handlers, effects, or browser APIs. Relevant sources:

- https://react.dev/learn/writing-markup-with-jsx
- https://react.dev/reference/react-dom/client/createRoot
- https://react.dev/reference/react/useState
- https://nextjs.org/docs/app/getting-started/server-and-client-components
- https://nextjs.org/docs/app/api-reference/directives/use-client
- https://nextjs.org/docs/app/api-reference/config/next-config-js
- https://nextjs.org/docs/app/api-reference/config/next-config-js/basePath
- https://nextjs.org/docs/15/pages/api-reference/config/typescript
- https://nextjs.org/docs/14/app/building-your-application/routing/route-handlers

NiceGUI uses a different model. Its official README describes NiceGUI as a
Python-based, backend-first UI framework built on FastAPI, Vue, Quasar, and
socket.io. Its Python element implementation creates element objects, registers
them with the client, serializes them to dictionaries, and sends updates through
the NiceGUI client/outbox model. Relevant sources:

- https://github.com/zauberzeug/nicegui
- https://nicegui.io/documentation/element
- https://nicegui.io/documentation/refreshable
- https://github.com/zauberzeug/nicegui/blob/main/nicegui/element.py
- https://github.com/zauberzeug/nicegui/blob/main/nicegui/client.py

This proves that React/shadcn JSX cannot be made valid by pasting it into
NiceGUI-style Python UI code. It must run inside a React build/runtime, or be
rewritten into NiceGUI/Vue semantics.

## Repository Evidence

The old server admin UI was invalid for this backend because its API calls did
not match the FastAPI contract:

- It calls `/auth/login`, but password login endpoints are intentionally absent.
- It calls `/auth/me`, while the backend exposes `GET /users/me`.
- It calls `/auth/refresh` without the required `{ "refresh_token": ... }`
  body.
- It referenced admin comments, featured-material toggles, and download stats
  endpoints that were not fully implemented before this rewrite.

The implementation now adds the missing FastAPI admin endpoints and accepts JSON
body input for `PUT /admin/users/{id}/role`, because the Next console sends
`{ "role": ... }` from a client component.

During implementation, this project's Next.js 14.2 build rejected
`next.config.ts` with `Configuring Next.js via 'next.config.ts' is not
supported`. Official Next TypeScript configuration docs list `next.config.ts`
support as added in Next.js `v15.0.0`; this project is pinned to Next `14.2.x`.
The rewritten console therefore uses `next.config.mjs` with
`basePath: "/console"` and `output: "standalone"`, matching the documented
sub-path deployment option and the Next 14 JavaScript config-file path.

Next App Router route handlers are valid for the BFF proxy and authentication
endpoints because the official Next 14 docs define route handlers as
`route.js|ts` files under `app/` and list `GET`, `POST`, `PUT`, and `DELETE`
among supported HTTP method exports.

## Decision

The desktop client will keep the PyQt plugin host but refactor `_mext` around
repositories, use cases, DTOs, lifecycle ownership, and localized UI text before
continuing visual polish.

The server will keep FastAPI and `/api/v1` as the stable API boundary. Missing
admin endpoints needed by the new console will be added explicitly.

The server admin console will be rewritten as a Next.js App Router application
using shadcn/ui-style local React components. The console will run under
`/console`, use existing OAuth/FIDO2/token authentication, and call FastAPI for
all business data.

## Consequences

- No business logic moves from FastAPI into the web UI.
- The React admin console requires a Next runtime or a deliberate static export
  strategy. The default deployment target is a Next standalone service behind
  Nginx.
- The client forum and web admin share API contracts but remain separate UI
  implementations.
- The desktop client forum API base URL is baked in `_mext.core.constants`.
  Runtime overrides through `MM_API_BASE_URL` or `.env` files are intentionally
  ignored so GitHub-built client packages do not depend on host-level
  environment variables.
- Future claims that a framework rewrite is valid must cite both framework
  runtime documentation and repository-specific build/API evidence.
