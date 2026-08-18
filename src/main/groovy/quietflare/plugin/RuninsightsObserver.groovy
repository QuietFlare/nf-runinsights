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

import java.nio.file.Files
import java.nio.file.Path
import java.nio.file.Paths
import java.nio.file.StandardOpenOption

import groovy.util.logging.Slf4j
import nextflow.Session
import nextflow.processor.TaskHandler
import nextflow.trace.TraceObserver
import nextflow.trace.TraceRecord

/**
 * Collects per-task resource metrics during the run, appends a run
 * summary to a durable cross-run history store, and reports how this
 * run compares to previous runs of the same pipeline.
 *
 * Pure observer: never alters pipeline behavior, and any failure in
 * insight generation is swallowed so it cannot fail a run.
 */
@Slf4j
class RuninsightsObserver implements TraceObserver {

    private Session session
    private final List<Map> tasks = Collections.synchronizedList(new ArrayList<Map>())

    // File-based heartbeat for debugging: logger output and stdout can both
    // be swallowed depending on the host pipeline's console mode, so during
    // development we write lifecycle marks straight to disk.
    private static void heartbeat(String msg) {
        try {
            def f = Paths.get(System.getProperty('user.home'), '.nf-runinsights', 'debug.log')
            Files.createDirectories(f.parent)
            Files.writeString(f, "${java.time.OffsetDateTime.now()} ${msg}\n",
                StandardOpenOption.CREATE, StandardOpenOption.APPEND)
        }
        catch( Exception ignored ) { }
    }

    @Override
    void onFlowCreate(Session session) {
        this.session = session
        heartbeat("flow-create run=${session?.runName}")
        log.debug "nf-runinsights: recording run metrics"
    }

    @Override
    void onProcessComplete(TaskHandler handler, TraceRecord trace) {
        if( tasks.isEmpty() ) heartbeat("first task-complete: ${trace.get('name')}")
        tasks << taskRecord(trace)
    }

    // note: cached tasks are deliberately not recorded, they did not
    // execute this run, so their historical timings would skew comparisons

    private static Map taskRecord(TraceRecord trace) {
        // submit/start are epoch millis; their gap is how long the task sat
        // in the executor queue before running, a key causal signal
        Long submit = trace.get('submit') as Long
        Long start  = trace.get('start') as Long
        [
            process     : trace.get('process') as String,
            name        : trace.get('name') as String,
            status      : trace.get('status') as String,
            realtime_ms : trace.get('realtime'),
            pcpu        : trace.get('%cpu'),
            peak_rss    : trace.get('peak_rss'),
            cpus        : trace.get('cpus'),
            memory_req  : trace.get('memory'),
            queue_ms    : (submit != null && start != null && start >= submit) ? start - submit : null,
            attempt     : trace.get('attempt'),
            container   : trace.get('container') as String,
            read_bytes  : trace.get('read_bytes'),
            write_bytes : trace.get('write_bytes'),
        ]
    }

    @Override
    void onFlowComplete() {
        heartbeat("flow-complete enter, tasks=${tasks.size()}")
        try {
            writeInsights()
            heartbeat("flow-complete done")
        }
        catch( Throwable e ) {
            // Throwable, not Exception: a linkage error (e.g. from an API
            // mismatch) must be reported too, never allowed to escape
            def sw = new StringWriter()
            e.printStackTrace(new PrintWriter(sw))
            heartbeat("flow-complete FAILED: ${e}\n${sw}")
            log.warn "nf-runinsights: could not write insights: ${e.message}"
        }
    }

    private void writeInsights() {
        if( !tasks ) {
            log.info "nf-runinsights: no executed tasks recorded (all cached or none ran)"
            return
        }

        def meta = session?.workflowMetadata
        String pipeline = meta?.projectName ?: meta?.scriptName ?: 'unknown'

        Map runRecord = [
            ts         : java.time.OffsetDateTime.now().toString(),
            run_name   : session?.runName,
            session_id : session?.uniqueId?.toString(),
            pipeline   : pipeline,
            processes  : InsightEngine.aggregate(new ArrayList<Map>(tasks)),
        ]

        def cfg = (session?.config?.get('runinsights') ?: [:]) as Map
        def store = HistoryStore.resolve(cfg.history as String)
        List<Map> prior = store.load(pipeline)   // load before saving: current run must not compare to itself
        store.save(runRecord)

        List<Map> findings = InsightEngine.compare(runRecord.processes as Map, prior)
        String report = InsightEngine.renderReport(runRecord, prior, findings)

        Path launchDir = (meta?.launchDir ?: Paths.get('.').toAbsolutePath()) as Path
        Path reportFile = launchDir.resolve('runinsights-report.md')
        Files.writeString(reportFile, report)

        println ""
        println "nf-runinsights: ${prior.size()} prior run(s) of '${pipeline}' in history"
        if( !prior )
            println "nf-runinsights: baseline recorded, comparisons start on your next run"
        findings.each { println "nf-runinsights: ${it.message}" }
        if( prior && !findings )
            println "nf-runinsights: no regressions detected vs history"

        // Optional AI narration, strictly best-effort: any failure leaves the
        // deterministic report exactly as written above.
        def aiCfg = (cfg.ai ?: [:]) as Map
        if( aiCfg.enabled ) {
            if( !AiNarrator.hasCredentials() ) {
                println "nf-runinsights: AI analysis skipped: set ANTHROPIC_API_KEY in the launch environment"
            }
            else {
                try {
                    String narrative = AiNarrator.narrate(runRecord, prior, findings, aiCfg)
                    String model = aiCfg.model ?: AiNarrator.DEFAULT_MODEL
                    Files.writeString(reportFile, report +
                        "\n## AI analysis\n\n${narrative}\n\n" +
                        "_Written by ${model} from the deterministic metrics above, verify against the numbers before acting._\n")
                    println "nf-runinsights: AI analysis added to report"
                }
                catch( Throwable e ) {
                    heartbeat("ai narrate FAILED: ${e}")
                    println "nf-runinsights: AI analysis skipped (${e.class.simpleName}: ${e.message?.take(120)}), deterministic report unaffected"
                }
            }
        }

        println "nf-runinsights: report: ${reportFile}"
    }

}
