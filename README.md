# Agent Skills

Kumpulan skill untuk coding agent (Pi, Claude Code, dan agent lain yang memakai format `SKILL.md`).

| Skill | Isi |
|---|---|
| [`fullstack-architecture`](fullstack-architecture/) | FastAPI + HTMX + Alpine.js + Jinja2 + MariaDB, lengkap dengan template project, addon, dan test suite. |
| [`fullstack-go-architecture`](fullstack-go-architecture/) | Go (net/http + html/template) + HTMX + Alpine.js + MariaDB. |

## Pemakaian

```bash
git clone https://github.com/zmutclik/agent-skills.git ~/.pi/agent/skills
```

Untuk Claude Code, symlink skill yang diinginkan:

```bash
ln -s ~/.pi/agent/skills/fullstack-architecture ~/.claude/skills/fullstack-architecture
```

Skill baru: buat folder baru berisi `SKILL.md` (frontmatter `name` + `description`).
