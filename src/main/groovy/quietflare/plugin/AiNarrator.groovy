/*
 * Copyright 2026, quietflare
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */
package quietflare.plugin

import com.anthropic.client.AnthropicClient
import com.anthropic.client.okhttp.AnthropicOkHttpClient
import com.anthropic.models.messages.MessageCreateParams
import groovy.json.JsonOutput
import groovy.util.logging.Slf4j

/**
 * Optional AI narration of the deterministic report.
 *
 * Division of labour is strict: the engine computes every number and
 * every finding; the model only explains, prioritises, and suggests
 * next steps from that computed summary. Privacy by construction -
 * the payload contains process names and resource statistics only,
 * never file paths, sample identifiers, or pipeline outputs.
 */
@Slf4j
class AiNarrator {

    static final String DEFAULT_MODEL = 'claude-opus-5'

    /** Cheap pre-check so we can skip cleanly before touching the SDK. */
    static boolean hasCredentials() {
        System.getenv('ANTHROPIC_API_KEY') || System.getenv('ANTHROPIC_AUTH_TOKEN')
    }

    static String narrate(Map runRecord, List<Map> prior, List<Map> findings, Map aiCfg) {
        String model = (aiCfg.model ?: DEFAULT_MODEL) as String

        // Process-level metadata only. History is compacted to the medians
        // the comparison used, so the payload stays small on long histories.
        def payload = [
            pipeline   : runRecord.pipeline,
            run_name   : runRecord.run_name,
            prior_runs : prior.size(),
            findings   : findings,
            processes  : runRecord.processes,
            history    : prior.takeRight(10).collect { run ->
                [run_name: run.run_name, ts: run.ts,
                 medians_ms: (run.processes ?: [:]).collectEntries { k, v ->
                     [k, v?.realtime_ms_median] }]
            },
        ]
        String data = JsonOutput.toJson(payload)
        if( data.length() > 12000 )
            data = data.take(12000) + '"...truncated"'

        String prompt = '''\
You are writing the "AI analysis" section of a pipeline performance report \
for nf-runinsights, a Nextflow plugin that benchmarks runs against their \
own history. The findings below were computed DETERMINISTICALLY by the \
plugin's engine, your job is to explain and prioritise them, not to \
re-derive or second-guess them.

Rules:
- Use ONLY numbers present in the data. Never invent, extrapolate, or round \
into new values.
- Lead with the single most actionable observation.
- If findings exist, explain what each means practically and what to try \
next (e.g. a config change with the process name).
- If there are no findings, say so briefly, a stable pipeline needs no drama.
- 120-200 words of plain prose or short bullets. No headings. Times are in \
milliseconds unless stated; format them readably.

DATA:
'''
        AnthropicClient client = AnthropicOkHttpClient.fromEnv()
        def params = MessageCreateParams.builder()
            .model(model)
            .maxTokens(1500L)
            .addUserMessage(prompt + data)
            .build()
        def response = client.messages().create(params)

        def sb = new StringBuilder()
        response.content().each { block -> block.text().ifPresent { sb.append(it.text()) } }
        String out = sb.toString().trim()
        if( !out )
            throw new IllegalStateException("model returned no text (stop_reason: ${response.stopReason()})")
        return out
    }
}
