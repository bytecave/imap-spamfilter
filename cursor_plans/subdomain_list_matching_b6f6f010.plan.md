---
name: Subdomain list matching
overview: Make a manual domain entry such as @apple.com match that host and every subdomain, for both allow and block, without changing address precedence or the Microsoft spoof rules.
todos:
  - id: matcher
    content: Match @host entries against that host and its subdomains in classify_list_hit, longer suffix wins within a rank
    status: completed
  - id: tests
    content: Add suffix, non-match, and longer-host precedence tests in test_address_lists.py
    status: completed
  - id: docs
    content: Update the dashboard hint, README matching sentence, and IMPLEMENTATION_STATUS precedence row
    status: completed
isProject: false
---

# Subdomain matching for domain list entries

`@apple.com` is stored and compared as an exact host today. [`classify_list_hit`](filter/filter.py) builds `@` + the address host and looks that string up, so `no_reply@email.apple.com` does not hit `@apple.com`. The same lookup is used for user lists and roster domain lists, and for allow and block.

## Match rule

A domain pattern `@example.com` matches when the From or Sender host is `example.com` or ends with `.example.com`. The dot boundary stays, so `notapple.com` and `apple.com.evil.com` do not match. No `*` syntax, no public-suffix special cases. Reply-To stays ignored. Stored patterns stay `@host`; this is a matcher change, not a migration.

Precedence stays in the same order:

- User address beats any domain pattern, including a parent domain.
- User `@host` beats any roster domain-list hit, even a longer suffix on the roster list.
- Roster address beats a roster domain pattern.
- Allow wins only on a true tie at the winning step.

Within one of those domain steps, the longer matching host wins. On the same list, block `@email.apple.com` beats allow `@apple.com` for `user@email.apple.com`, and the reverse allow beats the parent block. Same host listed as both allow and block is still a tie and allow wins.

Spoof handling does not change. A forged From or Sender can still match. Microsoft `compauth=fail` (or `dmarc=fail` when compauth is absent) still keeps Inbox mail and flags it in move mode, and still refuses a Junk rescue.

## Code

In [`filter/filter.py`](filter/filter.py) `classify_list_hit`, replace the exact `@host` set lookup with a suffix check over the domain-pattern rows already loaded. Rank numbers stay 4, 3, 2, 1 so existing tests keep their meaning. Compare `(rank, host length)` and let allow win only when both kind are present at that same pair.

Update the docstring there, the dashboard hint in [`filter/dashboard.py`](filter/dashboard.py) (the `allow_domain` line), the matching sentence in [`README.md`](README.md), and the precedence row in [`IMPLEMENTATION_STATUS.md`](IMPLEMENTATION_STATUS.md).

## Tests

Add cases in [`filter/test_address_lists.py`](filter/test_address_lists.py):

- `@apple.com` allow matches `a@apple.com` and `no_reply@email.apple.com`.
- It does not match `a@notapple.com` or `a@apple.com.evil.com`.
- The same suffix rule on a block entry.
- Longer host wins on one list: block `@email.apple.com` over allow `@apple.com`, and allow `@email.apple.com` over block `@apple.com`.
- An exact user address still beats a parent-domain entry of the opposite kind.
- A user `@apple.com` allow still beats a roster block of `spam@email.apple.com`.

Existing rank assertions stay as they are. Run the address-list tests. No rescan of mail already sitting in Inbox: the bookmark does not revisit those UIDs, so this applies on the next classification of a message.
