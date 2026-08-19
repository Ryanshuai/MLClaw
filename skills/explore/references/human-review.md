# Human visual review: how to make somebody's few minutes worth something

`SKILL.md` Stage 6.5 points here. The main document keeps only *when a person must be
called in*; **this is how to organise those minutes**.

---

#### The two rulers are complementary, not redundant

| | Resolution | Can it be wholesale wrong |
|---|---|---|
| **Numbers** | high — can separate 0.25 AP | ‼️ **Yes**, and confidently so: when they share assumptions with the implementation, they are wrong as a whole |
| **A person's eye** | low — cannot tell 5 mm from 8 mm | **Makes no errors of principle**: a box rotated 90°, a box straddling two cartons, two cartons read as one — a person sees it instantly |

So the order is not "call a person once the numbers run out". It is **the person rules out
errors of principle first, and the numbers then argue about resolution.** The reverse holds
just as firmly: **do not use a person's eye to adjudicate small differences** — their output
should be binary or ternary (is there an error of principle / which one catches more, blind),
never "about 5% better".

#### Triage: when numbers plus debate are enough

| Decision | What it looks like | What to use |
|---|---|---|
| **Simple** | same ruler, same geometric representation, one flag flipped, and the pre-registered metric moves the same way as AP | numbers + debate ①, **no person** |
| **Complex** | any one of the four below | **blind human review first**, numbers after |

The four that *require* a person (not "preferably"):

1. **The metric changed, the geometric representation changed, or the matching convention
   changed** — AABB → oriented is exactly this, and every historical number is void at that
   moment;
2. **The pre-registered metric and AP move in opposite directions** (one up, one down);
3. Role A in ⑤ re-classified something from "fatal" to "irrelevant";
4. **The last gate before a large T3 run.**

#### How to make those minutes worth something

- ‼️ **Choose the frames in advance, and always include a control group**: (a) ones the
  pre-registered metric says improved, (b) ones it says got worse, and (c) **randomly sampled
  control frames**, all three mixed together. Showing only the best and the worst is asking a
  person to rubber-stamp your conclusion.
- ‼️ **Blind it**: do not say which one is the new version. **"Independent ruler + built-in
  control + control group" applies to people exactly as it applies to scripts** — once
  somebody knows which is new, they will see what they expect to see.

#### Blind review needs two independent visual channels (a check digit on human feedback)

‼️ **A single-channel blind review cannot detect its own errors.** If the only distinction is
colour and the person says "the green one is better", you cannot tell a real conclusion from
somebody misremembering, from a reversed render mapping, from a transcription slip —
**and wrong feedback gets counted silently into the ratio.**

The rules:

- **Two channels that do not interfere**, e.g. A blue / B green (colour) **plus** A wavy /
  B dashed (line style). Colour + opacity **does not work** — opacity changes perceived
  colour, so the two channels stop being independent.
- **Re-randomise both channels independently for every frame** across (new, old). The mapping
  is stored and never shown to the reviewer.
- ‼️ **The person must report both channels**: "the green one, the dashed one, is better."
  The main agent checks that against the mapping: it counts only when the two channels agree,
  and **the frame is discarded when they disagree** — that is the error detection. Never fold
  a disagreeing frame into the blind-review ratio.
- **The discard rate is itself a metric**: a high one means the visualisation is broken (the
  two channels fight each other visually, or neither is salient enough) or the reviewer is
  skimming. Both must be fixed before any conclusion is discussed.
- **Do not use left/right position as an identity channel** — "the one on the left" is the
  easiest thing to cross wires on when describing something aloud, and it changes meaning
  after a scroll or a page turn. Randomise position anyway (to defeat positional bias), but
  never let it carry identity.
- ‼️ **A blind review's channels may not reuse the renderer's existing semantic colours.**
  In our rrd, green / red / yellow / white-dashed **already mean something** (matched / wrong
  box / weak match / where it should have been), so using them as A/B identity collides
  directly with reading the picture.
- **No more than ~20 frames at a time.** Past that people start skimming, and feedback quality
  falls faster than coverage rises.
- ‼️ **The person's judgement must become a count, written back to `findings.json`** with
  `source: "human"` and `value: 0.70, unit: "blind-review share preferring the new version",
  n: 20`. **Human feedback goes through the same interface, and that is what closes this
  pipeline's loop** — otherwise it is one sentence in a chat log saying "yeah, this one looks
  better", and nobody next round can compare it to today.
