/**
 * Bracket matching, with a guard against being handed a position that isn't one.
 *
 * THE PROBLEM
 *
 * Every arrow key press logged a caught exception:
 *
 *     TypeError: Cannot read properties of null (reading 'endSide')
 *         at RangeSetBuilder.addInner
 *         at RangeSet.of
 *         at Decoration.set
 *         at bracketMatchingState.update
 *
 * The values, read out of a paused debugger on the live site:
 *
 *     from: undefined   to: NaN   value: { class: "cm-nonmatchingBracket" }
 *
 * `RangeSetBuilder.addInner` opens with
 *
 *     let diff = from - this.lastTo || value.startSide - this.last.endSide
 *
 * and `undefined - lastTo` is NaN, which is falsy, so it evaluates the right
 * hand side - where `this.last` is still null on the builder's first add. The
 * TypeError is that null, and the undefined `from` is what routed it there.
 *
 * WHERE THE UNDEFINED COMES FROM
 *
 * `bracketMatchingState.update` calls `matchBrackets(state, range.head, ...)`
 * for each selection range, and `matchPlainBrackets` builds its result as
 * `{ from: pos, to: pos + 1 }` for a forward match. `from: undefined` with
 * `to: NaN` is exactly that shape with `pos` undefined - so `range.head` was
 * undefined for one of the ranges the field was given.
 *
 * Two upstream details conspire to turn that into a decoration rather than a
 * quiet no-match. `state.sliceDoc(undefined, NaN)` returns an empty string, and
 * `brackets.indexOf("")` is 0, not -1 - so the "is there a bracket here?" test
 * passes on a character that does not exist. The forward-direction guard that
 * follows then also passes, and a `nonmatchingBracket` decoration is built over
 * the undefined position.
 *
 * WHY THIS IS A GUARD AND NOT A FIX
 *
 * The undefined position originates above us: bisection showed the exception
 * survives removing both `yCollab` and our own `localCursor`, so stock
 * CodeMirror with these stock extensions produces it alone. We cannot correct
 * `@codemirror/language` from here, and vendoring it to change three lines
 * would be a much larger liability than this.
 *
 * What we can do is refuse to describe a decoration by a position that is not a
 * position. A match whose bounds are not finite, ordered document offsets is
 * not a match, so it renders nothing - which is the same thing the library
 * would have done had `indexOf("")` returned -1.
 */

import { bracketMatching, type MatchResult } from '@codemirror/language';
import type { Extension, Range } from '@codemirror/state';
import { Decoration } from '@codemirror/view';

// Same class names the library's own renderer uses, so the themes that style
// bracket matches - including CodeMirror's base theme and oneDark - still apply.
const matchingMark = Decoration.mark({ class: 'cm-matchingBracket' });
const nonmatchingMark = Decoration.mark({ class: 'cm-nonmatchingBracket' });

/** A real, orderable span of this document. */
function isRealRange(
  span: { from: number; to: number } | null | undefined,
  docLength: number,
): span is { from: number; to: number } {
  if (span == null) return false;
  const { from, to } = span;
  return (
    Number.isFinite(from) &&
    Number.isFinite(to) &&
    from >= 0 &&
    to >= from &&
    to <= docLength
  );
}

/**
 * `defaultRenderMatch`, with every bound checked before it is used.
 *
 * Deliberately drops only the offending span rather than the whole match: when
 * an opening bracket is real and its partner is not, highlighting the one we
 * can locate is still the right answer.
 */
export function guardedBracketMatching(): Extension {
  return bracketMatching({
    renderMatch: (match: MatchResult, state): readonly Range<Decoration>[] => {
      const mark = match.matched ? matchingMark : nonmatchingMark;
      const docLength = state.doc.length;
      const out: Range<Decoration>[] = [];
      if (isRealRange(match.start, docLength)) {
        out.push(mark.range(match.start.from, match.start.to));
      }
      if (isRealRange(match.end, docLength)) {
        out.push(mark.range(match.end.from, match.end.to));
      }
      return out;
    },
  });
}
