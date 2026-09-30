---
name: Outlook add-in setup
overview: "The Outlook spam add-in cannot be built or tested on the ByteLord VPS. Next work moves to the Windows desktop clone: install the VSTO build tools, then add a ClickOnce C# add-in that only moves and copies mail between the existing folders."
todos:
  - id: switch-desktop
    content: Continue implementation in Cursor on the Windows desktop clone, not the ByteLord SSH session.
    status: pending
  - id: install-buildtools
    content: Install VS 2022 Build Tools Office/SharePoint workload with recommended components (VSTO + .NET Framework 4.8 targeting pack) and confirm msbuild.
    status: pending
  - id: outlook-and-folders
    content: Confirm classic Outlook and the four training folders on a shadow test mailbox.
    status: pending
  - id: signing-cert
    content: Create a self-signed code-signing cert; keep the pfx outside the repo and trust it locally.
    status: pending
  - id: toolchain-gate
    content: Build and ClickOnce-publish a minimal VSTO project before writing the real ribbon.
    status: pending
  - id: addin-project
    content: Add outlook-addin/ with the four Home-tab actions, missing-folder errors, multi-select summary, and update checks disabled.
    status: pending
  - id: shadow-click-test
    content: Sideload on the desktop and click-test against a shadow mailbox, not rich_bytecave.
    status: pending
isProject: false
---

# Outlook spam add-in: where to build it

The two attached specs are [outlook_spam_addin_requirements.md](/home/bytecave/.cursor/projects/opt-bytelord-projects-imap-spamfilter/attachments/e736e73c-3969-4d31-88a1-2054b3b40640/outlook_spam_addin_requirements.md) and [outlook_addin_dev_environment_setup_guide.md](/home/bytecave/.cursor/projects/opt-bytelord-projects-imap-spamfilter/attachments/e736e73c-3969-4d31-88a1-2054b3b40640/outlook_addin_dev_environment_setup_guide.md). They describe a classic-Outlook VSTO COM add-in (.NET Framework 4.8, C#, ClickOnce, self-signed cert) whose only job is folder copy/move. It does not call the spam filter.

## Do not build this on the VPS

ByteLord is Linux. `msbuild`, `dotnet`, and Wine are not installed. Even if they were, this add-in cannot be compiled or run here:

- VSTO targets, ClickOnce signing, and the Office interop assemblies ship only with Visual Studio Build Tools on Windows (`Microsoft.VisualStudio.Workload.OfficeBuildTools`).
- The add-in loads inside classic desktop Outlook. New Outlook, Outlook on the web, and Outlook for Mac are out of scope (REQ-021, REQ-061).
- The code-signing certificate is created in the Windows certificate store.

Source text can be typed from any machine. Build, publish, certificate trust, and ribbon clicks have to happen on the Windows desktop where this repo is already cloned and classic Outlook is installed. The next implementation session should be Cursor opened on that desktop clone, not this SSH session.

The filter image is unaffected either way: [deploy/bytelord-compose.yaml](deploy/bytelord-compose.yaml) builds with context `filter/` only, so a new top-level add-in folder is not baked into `spamfilter`.

## Windows toolchain, before any add-in code

Follow the setup guide on the desktop, with one correction. The VSTO build tools and the .NET Framework 4.8 targeting pack are **recommended** components of the Office/SharePoint build-tools workload, not required ones. Installing the workload alone can omit them. Use Build Tools 2022 and include recommended components:

- Workload `Microsoft.VisualStudio.Workload.OfficeBuildTools` with `includeRecommended`
- That pulls in `Microsoft.VisualStudio.Component.TeamOffice.BuildTools` (VSTO) and `Microsoft.Net.Component.4.8.TargetingPack`

Then confirm, still on that machine:

- `msbuild -version` works.
- Outlook → File → Office Account → About Outlook is **classic** desktop Outlook, not the new Outlook toggle.
- The test mailbox already has `Inbox/Allowlist`, `Inbox/Blocklist`, `Junk Email/Train-Ham`, and `Junk Email/Train-Spam`. Create those by hand for the test mailbox only. The add-in must never create them (REQ-013, REQ-050, REQ-062).
- A self-signed code-signing cert in `Cert:\CurrentUser\My`, with the `.pfx` stored **outside the repo**. Do not use the guide's placeholder password, and do not commit the `.pfx` or `.cer`. Trust the public cert in LocalMachine Root and Trusted Publishers on each install machine (admin, once).

Gate: a tiny VSTO project must `msbuild /t:Build` and `msbuild /t:Publish` before the real ribbon is written (setup guide section 4). Build Tools can compile and publish without the Visual Studio GUI. If the add-in fails to load in Outlook, diagnosing that is much easier with Visual Studio Community's Office/SharePoint development workload; that IDE is optional, not required by the spec.

## What gets added to the repo

After the toolchain gate passes, add `outlook-addin/` in this repo (the desktop clone already has it) and copy both requirement markdown files into that folder so they travel with the code. Gitignore `bin/`, `obj/`, `publish/`, and `*.pfx`.

Behavior, matching the requirements:

- Home-tab ribbon group, four buttons (REQ-070): Not Junk, Junk, Allowlist, Blocklist.
- Resolve `Inbox` and `Junk Email` with `GetDefaultFolder` on the **store that owns the selected item**, then find the four subfolders by name (REQ-010, REQ-031). Never create, rename, or delete them.
- Not Junk: copy to `Junk Email/Train-Ham`; move the original to `Inbox` only when it is not already there (REQ-040–042).
- Junk: copy to `Junk Email/Train-Spam`, then move the original to `Junk Email` (REQ-043–044).
- Allowlist / Blocklist: move to `Inbox/Allowlist` or `Inbox/Blocklist` (REQ-045–046).
- Multi-select: each item independent; one failure does not stop the rest; one summary dialog at the end naming any missing folder (REQ-030–032, REQ-050–052).
- ClickOnce publish with updates disabled (REQ-072). Per-user manual install only (REQ-063). No HTTP calls (REQ-060).

Icons (REQ-071) come after the four actions work: simple custom ribbon images are enough if a search does not turn up a clean set.

## How this meets the live filter

The add-in never talks to rspamd or the dashboard. The existing IMAP loop already drains those folders: Train-Spam/Train-Ham learn and land in Trained-*; Allowlist learns ham and returns the message to Inbox; Blocklist learns spam and moves it to Junk.

Do not click-test against `rich@bytecave.net` first. That mailbox is in **move** mode, and retention has already started moving old Trained-* mail to Deleted Items (101 Trained-Spam and 500 Trained-Ham at 01:26 Pacific, with another ham batch about hourly). A ribbon click on that mailbox is a real Bayes learn. First ribbon test should be a **shadow** mailbox. Junk on a message that was in Inbox can also be learned twice (Train-Spam drain plus the filter seeing the Inbox→Junk move); watch that on the shadow mailbox and report it rather than changing the required copy-then-move.

Leave the VPS filter alone during this work: no image rebuild, no `accounts.yml` edit, no retention change, no Bayes wipe.
