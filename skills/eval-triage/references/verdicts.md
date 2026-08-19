# The four verdicts, and how to tell them apart

Read this before judging. The four are exhaustive over "the model scored badly on this sample", and
each has a different owner — so the judgement is not a description, it is a routing decision made
once, by someone looking at an image, and never checkable again from the record alone.

## `label_wrong` — the annotation is wrong

The model's output is right, or righter than the ground truth. **This is the pile the ranking is
biased towards**, and knowing that changes how to read a first pass: a wrong label is a target the
model cannot satisfy, so its score sits at the top of a worst-first ranking permanently, while a
merely hard sample drifts down as training improves.

Signals: the prediction looks correct to you and the GT does not; a box off by a category rather
than a margin; a class that is defensible either way (the annotation guide is ambiguous — still
`label_wrong`, and the fix is the guide); an object present in the image and absent from the GT.

**Never route these as hard examples.** Adding more samples like this adds more of the noise that
produced the ranking. `route` enforces it and writes the exclusion down.

The ambiguous-guide case deserves its own note: when two annotators would legitimately disagree, the
finding is about the *spec*, not the unit. Say so in `--basis` — `/data-label`'s send freezes a spec
snapshot, so a rework round can carry a corrected instruction rather than just the units.

## `sample_hard` — the label is right and the sample is genuinely difficult

Occlusion, motion blur, extreme scale, an unusual pose, a lighting condition that appears three
times in the whole set. **The only pile that legitimately becomes more data**, because the model's
failure here is a coverage problem: it has seen too few of these to learn them.

Signals: you can see the right answer but had to work at it; the same difficulty recurs across
several of the ranked cases (a cluster is much stronger evidence than a single hard image); the
prediction is *nearly* right rather than categorically wrong.

The honest bar: could a competent annotator get this right? If yes, and the model didn't, and the
sample is rare — `sample_hard`. If yes and the sample is *not* rare — look again at `model_wrong`.

## `model_wrong` — label right, sample ordinary, model still wrong

The sample is unremarkable, the label is correct, and there are plenty like it in the training set.
**More data does not fix this**, and this pile never leaves the model line.

Signals: the failure mode repeats on easy samples; a whole class is bad while its neighbours are
fine; the prediction is confidently wrong rather than uncertain; the error looks systematic
(everything shifted, everything one class over, small objects always missed).

Where it goes: `stages/training/config.json -> param_injection` and the training config — loss
weighting, augmentation, anchor or resolution settings, class balance, or capacity. Start at the
`evidence` line behind the relevant param, because a param the code ignores is the most common
reason a fix that should have worked did nothing.

**This is the distinction the skill exists to make**, and the one nothing can check afterwards.
`sample_hard` and `model_wrong` look identical in the per-sample record — same field, same score,
same class — and separating them is exactly the difference between "we need another 5000 images" and
"the loss weighting is wrong". Getting it wrong costs a labeling round that changes nothing.

## `unclear` — nobody could tell from looking

Not a hedge and not a failure. It is routed nowhere and reported, because **a pile of unclears is a
finding about the review, not about the model** — usually that the reviewer could not see enough,
which is a `per_sample.fields` problem: no overlay rendered, GT not carried across, class names
missing. Fix that in `output.json -> per_sample.fields` and re-rank.

Recording `unclear` honestly is worth more than a guess, for the reason every claim/verified split in
MLClaw exists: a guessed verdict is indistinguishable from a judged one the moment it is written
down, and the person acting on it cannot tell.

---

# Provenance — derived, never passed

| Value | What it means | How it happens |
|---|---|---|
| `unreviewed` | nobody has looked | the state `rank` leaves every case in. `route` refuses these |
| `claim` | one kind of source looked | one `judge`. Also two agent passes agreeing — see below |
| `verified` | two **different kinds** of source agree | `judge --by agent` then `confirm --by human --agree`, or anything plus `--by gold` |
| `disputed` | equal authority disagreed | no standing verdict; `route` refuses. Needs a third look |

`AUTHORITY` is `gold > human > agent`, and it is **authority on disagreement, not trustworthiness in
general**: a person overrules an agent about what is in an image, and a known answer overrules both.

**Two agent passes never reach `verified`.** They are one source sampled twice — their agreement
measures the model's consistency, not the label — so the provenance stays `claim` and
`corroborated_by_agent_only` goes into `caveats`. This is CLAUDE.md "Never let somebody's word become
a checked fact" applied to the agent, which the rule does not exempt.

**Overruled judgements are kept, never resolved away.** Whether the agent's calls can be trusted on
*this* dataset is answerable only from how often a person overruled it, and that number exists only
if the overrules survive in `caveats`. A record that silently replaced the agent's verdict with the
human's would look identical whether the agent was right 95% of the time or 40%.
