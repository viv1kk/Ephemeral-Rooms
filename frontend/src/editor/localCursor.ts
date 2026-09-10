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
 * ---------------------------------------------------------------------------
 * WHY THIS DRAWS INTO A LAYER AND NOT A WIDGET DECORATION
 * ---------------------------------------------------------------------------
 *
 * It used to be `Decoration.widget(...).range(head)`, and that made the editor
 * feel broken. An inline widget is a real stop for horizontal cursor motion, so
 * with the flag sitting at the caret, ArrowRight had to step over it before the
 * document offset would advance - and because the flag is rebuilt at the new
 * head on every update, that happened at every position, not once.
 *
 * Measured, two people in a room, nine ArrowRight presses:
 *
 *     with the widget     the cursor moved on 3 of 9 presses
 *     without it          9 of 9
 *
 * which matches the report exactly: it started when a second person put a
 * cursor in the document (this flag only draws when someone else is present),
 * it was much worse rightward (the widget sat on the side motion had to cross)
 * than leftward, vertical motion was unaffected (it moves by line, not by
 * character), and it did not clear when the other person left, because their
 * awareness entry - and so this flag - outlived them.
 *
 * A layer is how CodeMirror draws its own caret and selection: absolutely
 * positioned markers over the content, participating in neither the document
 * nor cursor motion. `RectangleMarker.forRange` puts them in exactly the place
 * the native caret would go, so this is the same machinery `drawSelection`
 * uses rather than a workaround.
 *
 * The label and the dot are `::after` and `::before` on the marker, so the flag
 * has no child nodes to rebuild and nothing here can affect layout of the text.
 *
 * Note what this deliberately does NOT do: subscribe to awareness itself. The
 * obvious implementation listens for awareness changes and dispatches a
 * transaction so the flag can re-render. That deadlocks the editor, because
 * y-codemirror publishes the local cursor from inside its own `update()`, so
 * the resulting awareness event re-enters CodeMirror mid-update. y-codemirror
 * already dispatches on awareness change, and the layer recomputes on every
 * update, so this stays correct without listening.
 */

import { EditorSelection, type Extension } from '@codemirror/state';
import { EditorView, layer, RectangleMarker } from '@codemirror/view';
import type { Awareness } from 'y-protocols/awareness';

/**
 * @param awareness  the document's awareness instance
 * @param color      reads the user's current colour. A getter rather than a
 *                   value on purpose: passing the colour itself would make it
 *                   part of the editor's construction dependencies, and a
 *                   reconnect that assigns a new identity would then tear down
 *                   and rebuild the whole EditorView - losing focus, selection
 *                   and scroll position.
 */
export function localCursor(awareness: Awareness, color: () => string): Extension {
  /** Is anyone else actually present in this document's editor? */
  const othersPresent = (): boolean => {
    for (const [clientId, state] of awareness.getStates()) {
      if (clientId === awareness.doc.clientID) continue;
      if ((state as { cursor?: unknown }).cursor != null) return true;
    }
    return false;
  };

  const caretLayer = layer({
    // Over the text, like the native caret, rather than behind it.
    above: true,
    class: 'cm-yLocalCaretLayer',

    markers: (view) => {
      // Only worth the clutter when there is someone to be distinguished from,
      // and only while the caret is actually in the editor.
      if (!view.hasFocus || !othersPresent()) return [];
      const { head } = view.state.selection.main;
      return RectangleMarker.forRange(
        view,
        'cm-yLocalCaret',
        EditorSelection.cursor(head),
      );
    },

    // The colour rides on the layer element rather than the marker, because a
    // RectangleMarker carries only a class name. Setting it here also means a
    // colour change costs a style write instead of rebuilding the marker.
    update: (_update, dom) => {
      dom.style.setProperty('--cm-local-caret-color', color());
      // Recomputed on every update rather than a filtered subset: the flag
      // depends on awareness, which changes outside CodeMirror's knowledge.
      // Markers are diffed by `eq`, so an unchanged caret touches no DOM.
      return true;
    },

    mount: (dom) => {
      dom.style.setProperty('--cm-local-caret-color', color());
    },
  });

  // Clearing on blur is what stops a parked caret sitting on other people's
  // screens; see the note at the top of this file.
  const clearOnBlur = EditorView.updateListener.of((update) => {
    if (!update.focusChanged || update.view.hasFocus) return;

    // CodeMirror's `hasFocus` is `document.hasFocus() && activeElement is the
    // content`, so it goes false for two quite different reasons: the user
    // clicked somewhere else on the page, or the whole tab went to the
    // background. Only the first means they left the text area.
    //
    // Treating them alike is wrong and was reported as a bug: switching tabs
    // to look something up erased your caret for everyone still working, and
    // took their "You" flag with it, since that only shows while someone else
    // has a cursor. Someone reading another tab is still parked where they
    // left off, so their caret should stay exactly where it is.
    if (!update.view.dom.ownerDocument.hasFocus()) return;

    if (awareness.getLocalState()?.['cursor'] != null) {
      awareness.setLocalStateField('cursor', null);
    }
  });

  return [caretLayer, clearOnBlur];
}
