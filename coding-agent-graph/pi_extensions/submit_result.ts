/**
 * Final structured result for the coding agent service.
 *
 * Pi calls submit_result as its last action. The arguments are validated against
 * the schema below, returned in the tool result's `details`, and `terminate: true`
 * ends the run without an extra LLM turn. pi_runner.py reads the result from the
 * `tool_execution_end` event instead of parsing JSON out of free text.
 *
 * CODING_AGENT_RESULT_STATUSES (comma-separated) restricts the allowed statuses
 * for the current phase, e.g. "ready,needs_input,failed" while planning.
 */

import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { StringEnum } from "@earendil-works/pi-ai";
import { Type } from "typebox";

const ALL_STATUSES = ["ready", "needs_input", "completed", "failed"];

function allowedStatuses(): string[] {
	const configured = (process.env.CODING_AGENT_RESULT_STATUSES ?? "")
		.split(",")
		.map((value) => value.trim())
		.filter((value) => ALL_STATUSES.includes(value));
	return configured.length ? configured : ALL_STATUSES;
}

export default function (pi: ExtensionAPI) {
	const statuses = allowedStatuses();

	pi.registerTool({
		name: "submit_result",
		label: "Submit Result",
		description:
			"Submit the final result of this task to the orchestrating service. This ends the run.",
		promptSnippet: "Submit the final structured result and end the run",
		promptGuidelines: [
			"Call submit_result exactly once, as your final action. Do not write the result as plain text or JSON in a message.",
			"After calling submit_result, do not emit another assistant response.",
		],
		parameters: Type.Object({
			status: StringEnum(statuses as [string, ...string[]], {
				description: `One of: ${statuses.join(", ")}`,
			}),
			summary: Type.String({ description: "Short summary of the outcome, under 300 characters" }),
			plan: Type.Array(Type.String(), { description: "Plan steps; empty when not planning" }),
			questions: Type.Array(Type.String(), {
				description: "Questions for the human; only with status needs_input",
			}),
			claimed_checks: Type.Array(Type.String(), {
				description: "Checks you ran or that should be run",
			}),
		}),

		async execute(_toolCallId, params) {
			if (!statuses.includes(params.status)) {
				throw new Error(`status must be one of: ${statuses.join(", ")}`);
			}
			if (params.status === "needs_input" && params.questions.length === 0) {
				throw new Error("status needs_input requires at least one question");
			}
			return {
				content: [{ type: "text", text: `Result submitted: ${params.status}` }],
				details: params,
				terminate: true,
			};
		},
	});
}
