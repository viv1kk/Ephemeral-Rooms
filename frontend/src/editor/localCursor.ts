/**
 * Your own caret, labelled "You", and cursor cleanup on blur.
 *
 * Two gaps in y-codemirror.next this fills:
 *
 * 1. It skips the local client entirely when drawing carets, on the reasoning
 *    that you already have a native caret. With several people editing, every
 *    other caret is a labelled colour flag and yours is a thin blinking line,
 *    which is the hardest one to find. This adds a matching flag for you.
 *
 * 2. Its blur handling does not work. The library computes
 *
 *        const hasFocus = view.hasFocus && document.hasFocus()
 *        const sel = hasFocus ? selection.main : null
 *        if (sel != null) { ...publish cursor... }
 *        else if (localState.cursor != null && hasFocus) { publish null }
 *
 *    The `else` branch is only reached when `hasFocus` is false, and then
 *    requires `hasFocus` to be true - so the cursor is never cleared. Leaving
 *    the editor leaves your caret parked on everyone else's screen for as long
 *    as the tab lives. This clears it on blur.
 *
 * Note what this deliberately does NOT do: subscribe to awareness itself. The
 * obvious implementation listens for awareness changes and dispatches a
 * transaction so the flag can re-render. That deadlocks the editor, because
 * y-codemirror publishes the local cursor from inside its own `update()`, so
 * the resulting awareness event re-enters CodeMirror mid-update. y-codemirror
 * already dispatches on awareness change, and every plugin sees every
 * transaction, so simply recomputing on each update is enough and safe.
 */

import type { Extension } from '@codemirror/state';
import {
  Decoration,
  type DecorationSet,
  EditorView,
  ViewPlugin,
  type ViewUpdate,
  WidgetType,
} from '@codemirror/view';
import type { Awareness } from 'y-protocols/awareness';

const JOINER = '⁠'; // word joiner, matching the library's caret markup

class LocalCaretWidget extends WidgetType {
  constructor(private readonly color: string) {
    super();
  }

  eq(other: LocalCaretWidget): boolean {
    return other.color === this.color;
  }

  toDOM(): HTMLElement {
    const caret = document.createElement('span');
    // The same classes as the remote carets, so one set of styles covers both,
    // plus a modifier for the couple of places they should differ.
    caret.className = 'cm-ySelectionCaret cm-yLocalCaret';
    caret.style.backgroundColor = this.color;
    caret.style.borderColor = this.color;

    const dot = document.createElement('div');
    dot.className = 'cm-ySelectionCaretDot';

    const label = document.createElement('div');
    label.className = 'cm-ySelectionInfo';
    label.textContent = 'You';

    caret.append(JOINER, dot, JOINER, label, JOINER);
    return caret;
  }

  updateDOM(): boolean {
    return false;
  }

  ignoreEvent(): boolean {
    return true;
  }
}

/**
 * @param awareness  the document's awareness instance
 * @param color      this user's colour, so their flag matches their remote one
 */
export function localCursor(awareness: Awareness, color: string): Extension {
  /** Is anyone else actually present in this document's editor? */
  const othersPresent = (): boolean => {
    for (const [clientId, state] of awareness.getStates()) {
      if (clientId === awareness.doc.clientID) continue;
      if ((state as { cursor?: unknown }).cursor != null) return true;
    }
    return false;
  };

  const build = (view: EditorView): DecorationSet => {
    // Only worth the clutter when there is someone to be distinguished from,
    // and only while the caret is actually in the editor.
    if (!view.hasFocus || !othersPresent()) return Decoration.none;
    const { head } = view.state.selection.main;
    return Decoration.set([
      Decoration.widget({ widget: new LocalCaretWidget(color), side: 1 }).range(head),
    ]);
  };

  const plugin = ViewPlugin.fromClass(
    class {
      decorations: DecorationSet;

      constructor(view: EditorView) {
        this.decorations = build(view);
      }

      // Recomputed on every update rather than on a filtered subset: the flag
      // depends on awareness, which changes outside CodeMirror's knowledge,
      // and the work is a single decoration.
      update(update: ViewUpdate): void {
        this.decorations = build(update.view);
      }
    },
    { decorations: (value) => value.decorations },
  );

  // Clearing on blur is what stops a parked caret sitting on other people's
  // screens; see the note at the top of this file.
  const clearOnBlur = EditorView.updateListener.of((update) => {
    if (!update.focusChanged || update.view.hasFocus) return;
    if (awareness.getLocalState()?.['cursor'] != null) {
      awareness.setLocalStateField('cursor', null);
    }
  });

  return [plugin, clearOnBlur];
}
