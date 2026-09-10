# claude-consumption-analysis

Tooling to investigate HG Insights' Claude Enterprise spend: who is spending,
on which model, through which surface — and whether the work needs a model at
all.

## ⚠️ This repo does not track analysis output

`profiles/`, `findings/` and `out/` are gitignored and must stay that way.
They contain employee names, verbatim excerpts from employees' actual
conversations, named customers and job candidates, and per-person cost data.

**Before every commit, check `git status` for files outside those folders that
carry the same content.** The gitignore protects the directories, not the
content pattern.

Analysis output lives on disk and is shared deliberately, not through git.
