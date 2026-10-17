"""The growth desk: agents that watch each brand's channels and steer what gets made next, with no one at the keyboard.

One run (python -m ytc.growth run --brand ankahi) goes through its agents in turn:

- collector: every post's numbers from Buffer (Instagram, Facebook and YouTube, as each platform reports them, so no
  Meta app is needed), the YouTube channel's daily analytics, what is booked for the coming days, and the newest
  YouTube comments;
- analyst: an age-fair score for each post, what the best and worst stories share (text, figures, hook, quiz, series,
  posting time), how each platform is trending, and what has gone wrong (gaps, failed posts, drops);
- listener: what commenters ask for, praise or complain about;
- scout: festivals coming up, and the Shorts in the brand's niche drawing the most views this week;
- strategist: a strong open-weights model (Qwen3.5 on OVH, which needs no key) or Gemini reads all of that and
  decides; a critic on another model checks the decisions, and fixed rules drop anything outside the brand's lines;
- executor: notes the writer reads before every script, the backlog's order, new stories aimed at what is working,
  a report in the repo, and a push to the owner's phone when something needs a person.

Every decision is logged with its reasons in decisions.jsonl. The desk never posts, deletes, edits a video or
replies to anyone: it changes only what the pipeline makes next and in what order.
"""
