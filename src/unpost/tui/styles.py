"""Shared Textual CSS."""

CSS = """
Screen { background: $surface; }
.banner { background: $warning; color: $text; text-style: bold; padding: 0 1; height: 1; }
.title { text-style: bold; padding: 1 1 0 1; }
.hint { color: $text-muted; padding: 0 1; }
.status { background: $boost; padding: 0 1; height: auto; }
.buttons { height: auto; padding: 1 1; }
.buttons Button { margin-right: 2; }
.error { color: $error; padding: 0 1; }
.ok { color: $success; padding: 0 1; }
.danger { color: $error; text-style: bold; padding: 0 1; }
DataTable { height: 1fr; }
ModalScreen { align: center middle; }
.dialog { width: 80%; max-width: 100; height: auto; max-height: 90%; border: thick $primary;
          background: $panel; padding: 1 2; }
.dialog Input { margin: 1 0; }
.dialog .buttons { padding: 1 0 0 0; }
#groups { width: 32; border-right: solid $primary; }
#log { height: 1fr; border: round $primary; }
#sections { height: 1fr; }
"""
