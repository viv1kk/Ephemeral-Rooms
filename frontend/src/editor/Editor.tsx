/**
 * CodeMirror 6 bound to a Yjs replica.
 *
 * Undo and redo are wired to the document's own UndoManager rather than
 * CodeMirror's history, so they only ever revert this user's operations
 * (spec section 10).
 */

import { useEffect, useMemo, useRef, useState } from 'react';
import { EditorState, type Extension, Compartment } from '@codemirror/state';
import {
  EditorView,
  drawSelection,
  highlightActiveLine,
  keymap,
  lineNumbers,
} from '@codemirror/view';
import { defaultKeymap, indentWithTab } from '@codemirror/commands';
import { searchKeymap, highlightSelectionMatches } from '@codemirror/search';
import { indentUnit, bracketMatching, foldGutter } from '@codemirror/language';
import { closeBrackets, closeBracketsKeymap } from '@codemirror/autocomplete';
import { oneDark } from '@codemirror/theme-one-dark';
import { yCollab } from 'y-codemirror.next';
import type { CollabDocument } from './collab';
import { LANGUAGES, languageById, languageForName, type LanguageOption } from './languages';
import { localCursor } from './localCursor';

interface EditorProps {
  collab: CollabDocument;
  documentName: string;
  userName: string;
  userColor: string;
}

const languageCompartment = new Compartment();

export function Editor({ collab, documentName, userName, userColor }: EditorProps): JSX.Element {
  const host = useRef<HTMLDivElement>(null);
  const view = useRef<EditorView | null>(null);
  // Held in a ref, not read directly in the effect below. A reconnect past the
  // reconnect grace assigns a new identity and therefore a new colour, and if
  // that were a dependency of the editor's construction the whole EditorView
  // would be rebuilt: focus lost, selection cleared, scroll reset. Since
  // y-codemirror only publishes your cursor while the editor has focus, the
  // rebuild also made you invisible to everyone else until you clicked back in.
  const colorRef = useRef(userColor);
  colorRef.current = userColor;
  const [override, setOverride] = useState<string | null>(null);

  const detected = useMemo(() => languageForName(documentName), [documentName]);
  const language: LanguageOption = override === null ? detected : languageById(override);

  // A manual override belongs to the document the user chose it for.
  useEffect(() => setOverride(null), [collab.documentId]);

  // Awareness carries the label and colour that y-codemirror.next renders on
  // remote cursors and selections (spec section 6).
  useEffect(() => {
    // `colorLight` paints the selection range and must be valid CSS: the
    // server emits hex precisely so this alpha suffix parses (`#rrggbb4d`).
    collab.awareness.setLocalStateField('user', { name: userName, color: userColor, colorLight: `${userColor}4d` });
  }, [collab, userName, userColor]);

  useEffect(() => {
    if (host.current === null) return undefined;

    const extensions: Extension[] = [
      lineNumbers(),
      foldGutter(),
      // Without this CodeMirror leaves the browser to paint the selection, and
      // the native highlight is opaque: it covers any remote selection it
      // overlaps, so a collaborator's highlight vanishes the moment you select
      // the same text. drawSelection renders it as a layer behind the content
      // instead, leaving the remote marks - which sit on the text itself -
      // visible through it.
      drawSelection(),
      highlightActiveLine(),
      highlightSelectionMatches(),
      bracketMatching(),
      closeBrackets(),
      indentUnit.of('  '),
      EditorView.lineWrapping,
      oneDark,
      languageCompartment.of([]),
      // The Yjs binding, including remote cursors. Passing the UndoManager
      // here is what scopes undo to this user.
      yCollab(collab.text, collab.awareness, { undoManager: collab.undoManager }),
      // yCollab deliberately skips your own caret, and never clears it on
      // blur; see editor/localCursor.ts.
      localCursor(collab.awareness, () => colorRef.current),
      keymap.of([
        ...closeBracketsKeymap,
        ...defaultKeymap,
        ...searchKeymap,
        indentWithTab,
        {
          key: 'Mod-z',
          run: () => {
            collab.undoManager.undo();
            return true;
          },
        },
        {
          key: 'Mod-y',
          mac: 'Mod-Shift-z',
          run: () => {
            collab.undoManager.redo();
            return true;
          },
        },
      ]),
    ];

    const editor = new EditorView({
      state: EditorState.create({ doc: collab.text.toString(), extensions }),
      parent: host.current,
    });
    view.current = editor;

    return () => {
      editor.destroy();
      view.current = null;
    };
  }, [collab]);

  // Language modes are lazy-loaded, so the initial bundle stays small.
  useEffect(() => {
    let cancelled = false;
    void language.load().then((extension) => {
      if (cancelled || view.current === null) return;
      view.current.dispatch({ effects: languageCompartment.reconfigure(extension) });
    });
    return () => {
      cancelled = true;
    };
  }, [language]);

  return (
    <div className="editor">
      <div className="editor-bar">
        <span className="editor-name">{documentName}</span>
        <label className="editor-language">
          Language
          <select
            value={language.id}
            onChange={(e) => setOverride(e.target.value)}
            aria-label="Syntax highlighting language"
          >
            {LANGUAGES.map((l) => (
              <option key={l.id} value={l.id}>
                {l.label}
              </option>
            ))}
          </select>
        </label>
      </div>
      <div className="editor-host" ref={host} data-testid="editor" />
    </div>
  );
}
