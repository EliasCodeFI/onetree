# The onetree diagrams, in words

Two animated diagrams explain onetree in the [README](../../README.md) and on the project's web
page. They are SVG files without scripts, external fonts or external requests, in a light and a
dark version. Each file carries its own title and description (the SVG `<title>` and `<desc>`
elements), and this page gives the same description step by step, so nothing is said only in a
picture. A viewer that prefers reduced motion gets one still picture: the final state.

## How onetree works

Files: [`how-onetree-works-light.svg`](how-onetree-works-light.svg) and
[`how-onetree-works-dark.svg`](how-onetree-works-dark.svg). A loop of 34 seconds in seven steps.

Animated diagram of onetree. Three coding agents share one git working tree. Each has a private index, claims the files it will write, and lands through a commit broker that audits the claims, runs a payload gate and the test suite, and then moves the branch.

1. **Sessions: a private index for every agent.** Each agent registers a session and gets its own git index. Staging is private; the working tree is shared. *In the picture:* Three agent cards (Agent A, Agent B, Agent C) stand on the left. In the middle is a panel titled "One shared working tree" with five files. On the right are the commit broker and the branch main. A box labelled "index" appears inside each agent card: each session has registered and received its own private git index. Three session_register events appear in the event log at the bottom.
2. **Claims: exclusive write leases on paths.** Before editing, a session claims the exact paths it will write. A claim is an exclusive lease on a path. *In the picture:* Agent A claims src/api.py, Agent B claims src/auth.py and Agent C claims tests/test_api.py. An arrow runs from each agent to its file, and the file row shows "claimed by A" (or B, or C) with a badge carrying the agent's letter. Three claim events are appended to the log.
3. **A refusal names the holder.** A claim on a held path is refused, and the refusal names the holder. A change to a path you did not claim cannot land. *In the picture:* Agent C also asks for src/api.py, which Agent A holds. The request is refused: the row shows "C refused: held by A" and a crossed-out badge with the letter C. A refused event is appended to the log.
4. **Edit in the shared tree, stage privately.** Agents edit their claimed files in the one working tree and stage them with ordinary git, each into its own index. *In the picture:* Each agent edits its own file in the shared working tree; the rows change to "edited by A", "edited by B" and "edited by C". Each agent then stages its file into its own index: the index box in each agent card now names the staged file.
5. **Landing: every change goes through the broker.** onetree land audits the claims, runs the admitted payload gate and your tests, then moves the branch ref by compare-and-swap. *In the picture:* Agent A lands. A chip labelled "candidate A" travels from A's index to the commit broker. The broker's four steps turn green one after another: claims audit, payload gate, test referee, compare-and-swap. A new commit marked A appears on the branch main, the file row shows "landed by A", and the events land_intent and landed are appended to the log.
6. **A red candidate stays out.** If the tests fail on the candidate, the landing is refused. The branch does not move, and the refusal is recorded. *In the picture:* Agent B tries to land. Its candidate passes the claims audit and the payload gate, but the test referee turns red: the tests fail on the candidate. The candidate is marked refused, a crossed-out circle shows that no commit was added to main, and a refused event is appended to the log.
7. **One tree, one history.** One clone, one working tree, one linear history. Every claim, landing and refusal is an event in an append-only log kept in git: refs/onetree/wal. *In the picture:* Agent C lands the same way as A: four green steps, and a second new commit, marked C, on main. The final picture: three agents, one working tree, a linear branch carrying the commits of A and C, B's change still waiting in the tree, and an event log with twelve entries.

## What `onetree land` does before a branch moves

Files: [`onetree-landing-path-light.svg`](onetree-landing-path-light.svg) and
[`onetree-landing-path-dark.svg`](onetree-landing-path-dark.svg). A loop of 32 seconds in five steps.

Animated diagram of a onetree landing. A candidate commit built from a session's private index passes four steps in order (claims audit, payload gate, test referee, compare-and-swap) before it becomes a commit on the branch main. One candidate with an unclaimed path is refused at the audit, and of two racing candidates one wins and the other is rebuilt and tried again.

1. **A candidate is built from the private index.** onetree land builds a candidate commit from the session's private index. The shared index and the working tree are not touched. *In the picture:* A row of boxes runs from left to right: a session with its private index, four numbered steps (claims audit, payload gate, test referee, compare-and-swap) and the branch main. A rail connects them. A token marked A, the candidate commit of Agent A, appears at the session's index.
2. **Four steps before the branch moves.** Each step can refuse. Only a candidate that passes all four moves the ref. A landing is recorded in the append-only log as an intent and a landed event. *In the picture:* The token A moves along the rail and stops at each step. Each step turns green as the candidate passes it: the claims audit, the payload gate, the test referee (shown working for a moment before it turns green) and the compare-and-swap. The token then moves onto the branch main and becomes a new commit marked A.
3. **One unclaimed path refuses it all.** If one changed path was not claimed by this session, the whole candidate is refused. Nothing reaches the branch. *In the picture:* A token marked B starts from the index. The index now lists a second file, src/db.py, marked "not claimed". The token stops at the first step, the claims audit, which turns red and shows the reason unclaimed_paths. The token is crossed out and disappears. The branch main is unchanged.
4. **A race has exactly one winner.** When two landings race, the compare-and-swap lets exactly one move the ref. The other is rebuilt on the new head and goes through the steps again. *In the picture:* Two tokens, C and B, travel through the first three steps one behind the other. At the compare-and-swap, C arrives first and becomes a new commit on main. For B the step turns amber: the head has moved. B travels back along a dashed return line labelled "moved head: rebuilt on the new head, tried again", passes all four steps again and becomes the next commit on main, after C.
5. **What reaches the branch.** Every commit on the branch is the exact candidate that was audited, gated and tested. A refused candidate leaves the branch where it was. *In the picture:* The final picture: all four steps are green and the branch main carries three new commits in the order A, C, B, after the commit it started with.

## The logo

Files: [`onetree-logo-light.svg`](onetree-logo-light.svg), [`onetree-logo-dark.svg`](onetree-logo-dark.svg),
the mark alone as [`onetree-mark-light.svg`](onetree-mark-light.svg) and
[`onetree-mark-dark.svg`](onetree-mark-dark.svg), the tab icon [`favicon.svg`](favicon.svg) and the
sharing card [`social-preview.png`](social-preview.png).

The mark is three lanes in blue, orange and purple, each starting at a dot, that merge into one
line, the way a commit graph is drawn: three sessions, one history. Beside it stands the word
onetree in lowercase letters, drawn as strokes rather than set in a font, so it looks the same
everywhere. The sharing card shows the logo above the line
"Your agents. One tree. No worktrees."
