# Outlook Spam Add-in — Requirements

## 1. Overview

A VSTO COM add-in for classic desktop Outlook that adds a custom ribbon group with four commands for triaging email against a pre-existing spam-training/filtering backend. The add-in's scope is limited to moving/copying the physical email items between folders — it does not call any backend API or service directly.

## 2. Mailbox Folder Structure

Every target mailbox has the following folders already provisioned. This structure is fixed — not configurable at runtime, not to be created or altered by the add-in.

```
Inbox
├── Allowlist
└── Blocklist

Junk Email
├── Train-Ham
└── Train-Spam
```

| ID | Requirement |
|---|---|
| REQ-010 | Folders are resolved relative to each mailbox's default Inbox and Junk Email folders (e.g., via the Outlook Object Model's `GetDefaultFolder(olFolderInbox)` / `olFolderJunk`), not by a fixed path or entry ID, so resolution works across mailboxes without hardcoding. |
| REQ-011 | `Allowlist` and `Blocklist` are subfolders of `Inbox`. |
| REQ-012 | `Train-Ham` and `Train-Spam` are subfolders of `Junk Email`. |
| REQ-013 | The add-in must never create, rename, move, or delete any of these folders. |

## 3. Architecture

| ID | Requirement |
|---|---|
| REQ-020 | Implemented as a VSTO (Visual Studio Tools for Office) COM add-in, C#. |
| REQ-021 | Targets classic desktop Outlook only — not new Outlook, not Outlook on the web, not Outlook for Mac. |
| REQ-022 | .NET Framework 4.8. |
| REQ-023 | UI is a custom Ribbon group with 4 buttons on the main Outlook Explorer ribbon (exact tab placement — Home tab vs. dedicated custom tab — is an implementation choice, see Section 7). |
| REQ-024 | Distributed via ClickOnce, per-user manual install, signed with a self-signed code-signing certificate (see setup guide). |

## 4. Ribbon Actions

| ID | Requirement |
|---|---|
| REQ-030 | All four actions must support multi-selected emails: a user can select multiple messages in the list view and invoke one button to apply the action to all selected items. |
| REQ-031 | All actions operate on the mailbox that owns the selected email, not necessarily the user's default mailbox — required for correct behavior in multi-account profiles. |
| REQ-032 | With multi-select, each selected item is processed independently; one item's failure does not abort processing of the rest of the batch (see REQ-051). |

### 4.1 Not Junk

| ID | Requirement |
|---|---|
| REQ-040 | Copy the email to `Junk Email\Train-Ham`. |
| REQ-041 | Move (not copy) the original email to `Inbox`, only if it is not already located in `Inbox`. |
| REQ-042 | If the email is already in `Inbox`, leave the original in place; the copy to `Train-Ham` (REQ-040) still occurs. |

### 4.2 Junk

| ID | Requirement |
|---|---|
| REQ-043 | Copy the email to `Junk Email\Train-Spam`. |
| REQ-044 | Move the original email to `Junk Email`. |

### 4.3 Allowlist

| ID | Requirement |
|---|---|
| REQ-045 | Move the email to `Inbox\Allowlist`. |

### 4.4 Blocklist

| ID | Requirement |
|---|---|
| REQ-046 | Move the email to `Inbox\Blocklist`. |

## 5. Error Handling

| ID | Requirement |
|---|---|
| REQ-050 | If any target folder (`Allowlist`, `Blocklist`, `Train-Ham`, `Train-Spam`, or the default `Inbox`/`Junk Email` folders themselves) cannot be found in the relevant mailbox, the add-in must not auto-create it. |
| REQ-051 | On a missing folder, show a clear error dialog identifying the missing folder by name and instructing the user to contact their admin; take no action on the affected email. |
| REQ-052 | In a multi-select batch, if some items succeed and others fail (e.g., inconsistent folder structure across mailboxes), process each item independently and present a summary of failures at the end rather than aborting the whole batch on the first error. |

## 6. Explicit Non-Goals / Out of Scope

| ID | Non-Goal |
|---|---|
| REQ-060 | No backend API calls, HTTP requests, or integration with the existing spam-training infrastructure from within the add-in. The add-in's only job is the physical folder move/copy; the existing backend infrastructure is assumed to act on the resulting folder contents independently (e.g., via rules, sync, or a separate process). |
| REQ-061 | No support for new Outlook, Outlook on the web, or Outlook for Mac (may be revisited later; not a current requirement). |
| REQ-062 | No automatic folder creation. |
| REQ-063 | No centralized/admin-pushed deployment (Intune, GPO) — per-user manual ClickOnce install only. |
| REQ-064 | No configurable/customizable folder names or paths — the folder topology in Section 2 is fixed. |

## 7. Additional Decisions

| ID | Requirement |
|---|---|
| REQ-070 | The 4-button ribbon group is placed on the Home tab (not a dedicated custom tab). |
| REQ-071 | Icon/button design for the four ribbon commands: source appropriate icons via web search, or create custom-drawn icons if no suitable existing icon is found. |
| REQ-072 | No auto-update via ClickOnce. Users manually download and install update packages; no update check (silent or prompted) runs at add-in startup. |
