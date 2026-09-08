/**
 * Syntax highlighting (spec section 9).
 *
 * Language is chosen from the document name's extension, with a manual
 * override in the UI. Every mode is dynamically imported so none of them land
 * in the initial bundle.
 */

import type { Extension } from '@codemirror/state';
import { StreamLanguage } from '@codemirror/language';

export interface LanguageOption {
  id: string;
  label: string;
  extensions: string[];
  load: () => Promise<Extension>;
}

export const LANGUAGES: LanguageOption[] = [
  {
    id: 'python',
    label: 'Python',
    extensions: ['py', 'pyw', 'pyi'],
    load: async () => (await import('@codemirror/lang-python')).python(),
  },
  {
    id: 'javascript',
    label: 'JavaScript',
    extensions: ['js', 'jsx', 'mjs', 'cjs'],
    load: async () => (await import('@codemirror/lang-javascript')).javascript({ jsx: true }),
  },
  {
    id: 'typescript',
    label: 'TypeScript',
    extensions: ['ts', 'tsx', 'mts'],
    load: async () =>
      (await import('@codemirror/lang-javascript')).javascript({ jsx: true, typescript: true }),
  },
  {
    id: 'c',
    label: 'C',
    extensions: ['c', 'h'],
    load: async () => (await import('@codemirror/lang-cpp')).cpp(),
  },
  {
    id: 'cpp',
    label: 'C++',
    extensions: ['cpp', 'cc', 'cxx', 'hpp', 'hh'],
    load: async () => (await import('@codemirror/lang-cpp')).cpp(),
  },
  {
    id: 'java',
    label: 'Java',
    extensions: ['java'],
    load: async () => (await import('@codemirror/lang-java')).java(),
  },
  {
    id: 'html',
    label: 'HTML',
    extensions: ['html', 'htm'],
    load: async () => (await import('@codemirror/lang-html')).html(),
  },
  {
    id: 'css',
    label: 'CSS',
    extensions: ['css', 'scss', 'less'],
    load: async () => (await import('@codemirror/lang-css')).css(),
  },
  {
    id: 'json',
    label: 'JSON',
    extensions: ['json', 'jsonc'],
    load: async () => (await import('@codemirror/lang-json')).json(),
  },
  {
    id: 'yaml',
    label: 'YAML',
    extensions: ['yaml', 'yml'],
    load: async () => (await import('@codemirror/lang-yaml')).yaml(),
  },
  {
    id: 'markdown',
    label: 'Markdown',
    extensions: ['md', 'markdown'],
    load: async () => (await import('@codemirror/lang-markdown')).markdown(),
  },
  {
    id: 'sql',
    label: 'SQL',
    extensions: ['sql'],
    load: async () => (await import('@codemirror/lang-sql')).sql(),
  },
  {
    id: 'shell',
    label: 'Shell',
    extensions: ['sh', 'bash', 'zsh', 'fish'],
    load: async () =>
      StreamLanguage.define((await import('@codemirror/legacy-modes/mode/shell')).shell),
  },
  {
    id: 'plain',
    label: 'Plain text',
    extensions: ['txt', 'text', 'log'],
    load: async () => [],
  },
];

const BY_EXTENSION = new Map<string, LanguageOption>();
for (const language of LANGUAGES) {
  for (const ext of language.extensions) BY_EXTENSION.set(ext, language);
}

export const PLAIN = LANGUAGES[LANGUAGES.length - 1]!;

/** Pick a language from a document name. Falls back to plain text. */
export function languageForName(name: string): LanguageOption {
  const dot = name.lastIndexOf('.');
  if (dot === -1 || dot === name.length - 1) return PLAIN;
  return BY_EXTENSION.get(name.slice(dot + 1).toLowerCase()) ?? PLAIN;
}

export function languageById(id: string): LanguageOption {
  return LANGUAGES.find((l) => l.id === id) ?? PLAIN;
}
