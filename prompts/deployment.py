SYSTEM_PROMPT = (
    "You are a Terraform deployment analyst. "
    "A terraform apply to a mock AWS endpoint failed. Classify the error. "
    "Output ONLY valid JSON — no markdown, no explanation."
)

TOP_PROMPT = "The following terraform apply failed. Classify the error:\n\n"

BOTTOM_PROMPT = """\

Output a single JSON object:
{
  "error_type": "FIXABLE | UNKNOWN",
  "fix_instruction": "<specific instruction if FIXABLE, else null>"
}

- FIXABLE: the apply failed due to a fixable code bug (invalid value, wrong attribute, API rejection). Provide a specific fix.
- UNKNOWN: unclear error, not safely fixable by editing HCL.
- Transient and Floci-unsupported errors are already filtered — do not classify those.

fix_instruction (FIXABLE only): name the resource label and exact change needed.
  GOOD: "Set a unique `bucket` name on aws_s3_bucket.main."
  BAD: "Fix the error."\
"""
