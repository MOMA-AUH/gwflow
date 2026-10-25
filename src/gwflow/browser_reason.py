"""Expandable Task reasons rendered only from captured inspection evidence."""

from pathlib import Path

from rich.text import Text
from textual import on
from textual.containers import Vertical
from textual.widgets import Collapsible, Static

from .browser import VimScroll
from .presentation import format_reason


class TaskReason(Vertical):
    DEFAULT_CSS = """
    TaskReason { height: auto; }
    TaskReason Collapsible { padding: 0; border: none; background: transparent; }
    TaskReason Collapsible > Contents { padding: 0; }
    TaskReason .reason-evidence { height: auto; max-height: 12; }
    TaskReason .reason-evidence:focus { border-left: wide $accent; }
    """

    def __init__(self, task, storage):
        super().__init__(id='task-reason')
        self.observation, self.storage = task, storage or {}
        self.expanded = False
        self.full_paths = False

    def compose(self):
        task = self.observation
        reason = task['reason']
        changes = task.get('input_changes', {})
        detailed = bool(changes or task.get('previous_results') or len(reason) > 140 or '\n' in reason)
        summary = task.get('reason_summary', reason) if detailed else reason
        fresh = task.get('fresh_attempt_required')
        if changes and fresh:
            summary = f"Input metadata changed ({len(changes)} file{'s' if len(changes) != 1 else ''})."
        elif fresh:
            summary = summary.removesuffix('; fresh attempt required')
        if detailed and len(summary) > 140:
            summary = summary[:137].rsplit(' ', 1)[0] + '…'
        summary = summary.replace('see --details', 'expand details')
        text = Text.assemble(('Reason:', 'bold'), ' ', format_reason(summary))
        if fresh:
            text.append('\nFresh attempt required')
            if task.get('previous_results'):
                text.append('; previous results must be removed before submission')
            text.append('.')
        elif task.get('previous_results'):
            text.append('\nPrevious results must be removed before submission.')
        yield Static(text)
        if detailed:
            with Collapsible(title=self.details_title(), collapsed=not self.expanded, id='reason-details'):
                with VimScroll(classes='reason-evidence'):
                    if changes:
                        yield Static(self.input_evidence(changes))
                    previous = task.get('previous_results')
                    for explanation in task.get('reason_details') or ([] if changes and fresh else [reason]):
                        yield Static(Text(format_reason(explanation)))
                    if previous:
                        yield Static(Text.assemble(('\nBefore submission\n', 'bold'),
                                                   'Previous results must be removed from:\n',
                                                   self.relative_path(previous)))
                    if changes or previous:
                        with Collapsible(title='Hide full paths' if self.full_paths else 'Show full paths',
                                         collapsed=not self.full_paths, id='reason-paths'):
                            paths = Text('Workflow directory: ' + self.storage.get('working_dir', 'unavailable') + '\n')
                            for path, change in changes.items():
                                paths.append('\nInput: ' + path + '\n')
                                paths.append('Old resolved path: ' + change['previous']['resolved'] + '\n')
                                paths.append('New resolved path: ' + change['current']['resolved'] + '\n')
                            if previous:
                                paths.append('\nPrevious results: ' + previous + '\n')
                            paths.append('\nCaptured reason\n', style='bold')
                            paths.append(reason)
                            yield Static(paths, id='reason-full-paths')

    def relative_path(self, path):
        base = self.storage.get('working_dir')
        if base:
            try:
                return str(Path(path).relative_to(base))
            except ValueError:
                pass
        return path

    def input_evidence(self, changes):
        text = Text('Changed external inputs\n', style='bold')
        text.append('Paths relative to the workflow directory where possible.\nOld → New\n', style='not bold')
        folder = None
        for path, change in sorted(changes.items()):
            parent = str(Path(path).parent)
            if parent != folder:
                text.append('\nFolder: ' + self.relative_path(parent) + '\n', style='not bold')
                folder = parent
            text.append('\n' + Path(path).name + '\n')
            previous, current = change['previous'], change['current']
            text.append(f"  Size (bytes): {previous['size']:,} → {current['size']:,}\n", style='not bold')
            text.append(f"  mtime_ns: {previous['mtime_ns']} → {current['mtime_ns']}\n", style='not bold')
            if previous['resolved'] != current['resolved']:
                text.append('  Old resolved path: ' + self.relative_path(previous['resolved']) + '\n', style='not bold')
                text.append('  New resolved path: ' + self.relative_path(current['resolved']) + '\n', style='not bold')
        return text

    def details_title(self):
        title = 'Hide details' if self.expanded else 'Show details'
        count = len(self.observation.get('input_changes', {}))
        return title + (f" · {count} changed file{'s' if count != 1 else ''}" if count else '')

    @on(Collapsible.Toggled)
    def toggle_details(self, event):
        if event.collapsible.id == 'reason-details':
            self.expanded = not event.collapsible.collapsed
            event.collapsible.title = self.details_title()
        elif event.collapsible.id == 'reason-paths':
            self.full_paths = not event.collapsible.collapsed
            event.collapsible.title = 'Hide full paths' if self.full_paths else 'Show full paths'
        title = event.collapsible.query_one('CollapsibleTitle')
        if title.has_focus:
            self.call_after_refresh(title.scroll_visible, animate=False)

    async def update_reason(self, task, storage):
        self.observation, self.storage = task, storage or {}
        await self.recompose()
