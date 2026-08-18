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

/**
 * Pure logic for nf-runinsights: aggregates per-task metrics into
 * per-process summaries, compares a run against history, and renders
 * the report. Deliberately free of Nextflow types so it can be unit
 * tested without a running session.
 */
class InsightEngine {

    // a change must be at least this big (ms) before we call it a
    // regression/improvement, so sub-second jitter never triggers flags
    static final long MIN_DELTA_MS = 2000

    static BigDecimal median(List values) {
        def nums = (values ?: []).findAll { it != null }.collect { it as BigDecimal }
        if( !nums ) return null
        def sorted = nums.sort()
        int n = sorted.size()
        return n % 2 == 1
            ? sorted[n.intdiv(2)]
            : ((sorted[n.intdiv(2) - 1] + sorted[n.intdiv(2)]) / 2)
    }

    /** Aggregate raw task records into one summary map per process. */
    static Map<String, Map> aggregate(List<Map> tasks) {
        def result = [:]
        tasks.groupBy { it.process }.each { proc, recs ->
            def rt  = recs.collect { it.realtime_ms }.findAll { it != null }.collect { it as Long }
            def rss = recs.collect { it.peak_rss }.findAll { it != null }.collect { it as Long }
            def q   = recs.collect { it.queue_ms }.findAll { it != null }.collect { it as Long }
            def rb  = recs.collect { it.read_bytes }.findAll { it != null }.collect { it as Long }
            def eff = recs.findAll { it.pcpu != null && it.cpus }
                          .collect { (it.pcpu as double) / (100d * (it.cpus as int)) }
            result[proc] = [
                tasks              : recs.size(),
                failed             : recs.count { it.status != 'COMPLETED' },
                realtime_ms_median : median(rt),
                realtime_ms_max    : rt ? rt.max() : null,
                peak_rss_max       : rss ? rss.max() : null,
                cpus_req           : recs.first().cpus,
                memory_req         : recs.first().memory_req,
                queue_ms_median    : median(q),
                cpu_eff_median     : eff ? median(eff) : null,
                read_bytes_total   : rb ? rb.sum() : null,
                retried            : recs.count { ((it.attempt ?: 1) as int) > 1 },
                container          : recs.collect { it.container }.find { it },
            ]
        }
        return result
    }

    /**
     * Compare current per-process summaries against prior runs of the
     * same pipeline; returns findings (regressions, improvements,
     * over-provisioning, failures).
     */
    static List<Map> compare(Map<String, Map> current, List<Map> prior) {
        def findings = []
        def ratios = []   // every process's cur/hist time ratio, for the environment rule
        current.each { proc, cur ->
            def hist = prior.collect { run -> (run.processes ?: [:])[proc] }.findAll { it != null }
            def base = median(hist.collect { it.realtime_ms_median })

            if( base != null && cur.realtime_ms_median != null && base > 0 ) {
                double curRt  = (cur.realtime_ms_median as BigDecimal).doubleValue()
                double baseRt = base.doubleValue()
                double ratio  = curRt / baseRt
                ratios << ratio
                if( ratio >= 1.5 && curRt - baseRt >= MIN_DELTA_MS )
                    findings << [type: 'regression', severity: 'warn', process: proc,
                        message: "${proc}: ${fmtDuration(curRt)} vs ${fmtDuration(baseRt)} median over ${hist.size()} prior run(s), ${String.format(Locale.ROOT, '%.1f', ratio)}x slower" +
                                 causeHints(cur, hist, curRt, baseRt)]
                else if( ratio <= 0.66 && baseRt - curRt >= MIN_DELTA_MS )
                    findings << [type: 'improvement', severity: 'info', process: proc,
                        message: "${proc}: ${fmtDuration(curRt)} vs ${fmtDuration(baseRt)} median, ${String.format(Locale.ROOT, '%.1f', 1 / ratio)}x faster"]
            }

            if( cur.retried )
                findings << [type: 'retries', severity: 'warn', process: proc,
                    message: "${proc}: ${cur.retried} of ${cur.tasks} task(s) needed retries, failures inflate wall time and cost"]

            // over-provisioning: peak memory never came near the request,
            // across every recorded run of this process
            def memReq = cur.memory_req == null ? null : cur.memory_req as Long
            def rssAll = (hist.collect { it.peak_rss_max } + [cur.peak_rss_max])
                            .findAll { it != null }.collect { it as Long }
            if( memReq && rssAll && rssAll.max() < memReq * 0.25 )
                findings << [type: 'overprovision', severity: 'info', process: proc,
                    message: "${proc}: peak memory ${fmtBytes(rssAll.max())} never exceeded 25% of the ${fmtBytes(memReq)} requested (${rssAll.size()} sample(s))"]

            if( cur.failed )
                findings << [type: 'failures', severity: 'warn', process: proc,
                    message: "${proc}: ${cur.failed} of ${cur.tasks} task(s) did not complete"]
        }

        // Environment rule: when MOST processes slowed together, the cause is
        // the machine/cluster/storage, not any single tool, and per-process
        // regression flags would mislead. A diffuse slowdown can trip this
        // even when no single process crosses its own threshold.
        if( ratios.size() >= 4 ) {
            int slowed = ratios.count { it >= 1.3 }
            if( slowed >= Math.ceil(ratios.size() * 0.6d) )
                findings.add(0, [type: 'environment', severity: 'warn', process: '*',
                    message: "pipeline-wide slowdown: ${slowed} of ${ratios.size()} processes are >=1.3x slower than history, this points at the machine, cluster load, or storage, not at any single tool"])
        }
        return findings
    }

    /**
     * Deterministic causal triage for a flagged regression: attribute the
     * slowdown to queue wait, a tool/container change, or input-data growth
     * where the recorded evidence supports it. Older history entries without
     * these fields simply contribute no hint.
     */
    private static String causeHints(Map cur, List hist, double curRt, double baseRt) {
        def hints = []

        def baseQ = median(hist.collect { it.queue_ms_median })
        if( baseQ != null && cur.queue_ms_median != null ) {
            double qDelta = (cur.queue_ms_median as BigDecimal).doubleValue() - baseQ.doubleValue()
            if( qDelta >= 0.5d * (curRt - baseRt) && qDelta > 0 )
                hints << 'mostly queue wait (scheduler/cluster load), not slower execution'
        }

        def prevContainer = hist.reverse().collect { it.container }.find { it }
        if( cur.container && prevContainer && cur.container != prevContainer )
            hints << "container changed (${prevContainer} → ${cur.container})"

        def baseB = median(hist.collect { it.read_bytes_total })
        if( baseB != null && cur.read_bytes_total != null && baseB > 0 && baseRt > 0 ) {
            double bytesRatio = (cur.read_bytes_total as BigDecimal).doubleValue() / baseB.doubleValue()
            if( bytesRatio > 1.2d && bytesRatio >= 0.7d * (curRt / baseRt) )
                hints << "input data grew ${String.format(Locale.ROOT, '%.1f', bytesRatio)}x, throughput is roughly flat, so this is growth, not a regression"
        }

        return hints ? ", likely cause: ${hints.join('; ')}" : ''
    }

    static String renderReport(Map run, List<Map> prior, List<Map> findings) {
        def sb = new StringBuilder()
        sb << "# nf-runinsights report\n\n"
        sb << "- **Pipeline:** ${run.pipeline}\n"
        sb << "- **Run:** ${run.run_name}\n"
        sb << "- **Time:** ${run.ts}\n"
        sb << "- **Prior runs in history:** ${prior.size()}\n\n"

        sb << "## Findings\n\n"
        if( !prior )
            sb << "_First recorded run, baseline established; comparisons start next run._\n"
        else if( !findings )
            sb << "_No regressions or anomalies vs history._\n"
        findings.each { sb << "- **${it.type}** (${it.severity}): ${it.message}\n" }

        sb << "\n## Processes (this run vs history)\n\n"
        sb << "| Process | Tasks | Median time | Hist. median | Peak RSS | Mem req |\n"
        sb << "|---|---|---|---|---|---|\n"
        run.processes.each { proc, cur ->
            def base = median(prior.collect { (it.processes ?: [:])[proc]?.realtime_ms_median })
            sb << "| ${proc} | ${cur.tasks} | ${fmtDuration(cur.realtime_ms_median)} | ${fmtDuration(base)} | ${fmtBytes(cur.peak_rss_max)} | ${fmtBytes(cur.memory_req)} |\n"
        }
        return sb.toString()
    }

    static String fmtDuration(Number ms) {
        if( ms == null ) return '-'
        double s = ms.doubleValue() / 1000
        if( s < 60 ) return String.format(Locale.ROOT, '%.1fs', s)
        long m = (long) (s / 60)
        return "${m}m ${String.format(Locale.ROOT, '%.0f', s - m * 60)}s"
    }

    static String fmtBytes(Number bytes) {
        if( bytes == null ) return '-'
        double v = bytes.doubleValue()
        for( unit in ['B', 'KB', 'MB', 'GB', 'TB'] ) {
            if( v < 1024 || unit == 'TB' )
                return String.format(Locale.ROOT, v < 10 && unit != 'B' ? '%.1f %s' : '%.0f %s', v, unit)
            v /= 1024
        }
        return null // unreachable
    }
}
