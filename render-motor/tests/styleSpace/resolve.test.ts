import { describe, expect, it } from "vitest";

import { getTheme, THEMES } from "../../src/styleSpace/visualStyles";

// Pure-logic smoke: theme lookup is deterministic and fails fast (no silent
// fallback, per code-standards). Replace/extend with withCosmetic + routing.
describe("getTheme", () => {
  it("returns a known theme by id", () => {
    const first = THEMES[0];
    expect(getTheme(first.id).id).toBe(first.id);
  });

  it("throws on an unknown id rather than masking it", () => {
    expect(() => getTheme("__does_not_exist__")).toThrow(/unknown visual style/);
  });
});
