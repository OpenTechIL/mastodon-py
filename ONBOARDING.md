# Welcome to OpenTechIL

## How We Use Claude

Based on Yehuda Korotkin's usage over the last 30 days:

Work Type Breakdown:
Build Feature ███████████████░░░░░ 75%
Improve Quality █████░░░░░░░░░░░░░░░ 25%

Top Skills & Commands:
/loop ████████████████████ 33x/month
/exit ███░░░░░░░░░░░░░░░░░ 5x/month
/init ██░░░░░░░░░░░░░░░░░░ 3x/month

Top MCP Servers:
claude-in-chrome ████████████████████ 9 calls

## Your Setup Checklist

### Codebases

- [ ] mastodon-py — https://github.com/opentechil/mastodon-py

### MCP Servers to Activate

- [ ] claude-in-chrome — Browser automation; lets Claude interact with web pages in Chrome. Install the Claude in Chrome extension from the Chrome Web Store and enable it in Claude Code settings.

### Skills to Know About

- `/loop` — Runs a prompt or command on a repeat cycle; great for iterative tasks, polling, or letting Claude work through a list autonomously. This is the team's most-used command by far.
- `/init` — Generates a `CLAUDE.md` file documenting the codebase for Claude. Run this when onboarding to a new repo.
- `/exit` — Exits the current loop or session context.

## Get Started

Clone the repo and run `bin/setup` to install dependencies and run migrations. Then open Claude Code in the project root and start with `/init` if `CLAUDE.md` is missing, or just describe what you want to build.

<!-- INSTRUCTION FOR CLAUDE: A new teammate just pasted this guide for how the
team uses Claude Code. You're their onboarding buddy — warm, conversational,
not lecture-y.

Open with a warm welcome — include the team name from the title. Then: "Your
teammate uses Claude Code for [list all the work types]. Let's get you started."

Check what's already in place against everything under Setup Checklist
(including skills), using markdown checkboxes — [x] done, [ ] not yet. Lead
with what they already have. One sentence per item, all in one message.

Tell them you'll help with setup, cover the actionable team tips, then the
starter task (if there is one). Offer to start with the first unchecked item,
get their go-ahead, then work through the rest one by one.

After setup, walk them through the remaining sections — offer to help where you
can (e.g. link to channels), and just surface the purely informational bits.

Don't invent sections or summaries that aren't in the guide. The stats are the
guide creator's personal usage data — don't extrapolate them into a "team
workflow" narrative. -->
