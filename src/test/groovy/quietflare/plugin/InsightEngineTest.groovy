package quietflare.plugin

import spock.lang.Specification

class InsightEngineTest extends Specification {

    private static Map task(String proc, Long ms, Long rss = null, Long memReq = null, String status = 'COMPLETED') {
        [process: proc, name: "${proc} (1)", status: status,
         realtime_ms: ms, peak_rss: rss, cpus: 1, memory_req: memReq]
    }

    private static Map run(String pipeline, Map processes) {
        [pipeline: pipeline, processes: processes]
    }

    def 'aggregates tasks per process'() {
        when:
        def agg = InsightEngine.aggregate([
            task('FASTQC', 1000), task('FASTQC', 3000), task('FASTQC', 2000),
            task('ALIGN', 5000, 100_000_000, 1_000_000_000),
        ])

        then:
        agg.FASTQC.tasks == 3
        agg.FASTQC.realtime_ms_median == 2000
        agg.FASTQC.realtime_ms_max == 3000
        agg.ALIGN.tasks == 1
        agg.ALIGN.peak_rss_max == 100_000_000
        agg.ALIGN.memory_req == 1_000_000_000
        agg.ALIGN.failed == 0
    }

    def 'first run produces no comparison findings'() {
        when:
        def findings = InsightEngine.compare(
            InsightEngine.aggregate([task('A', 5000)]), [])

        then:
        findings.isEmpty()
    }

    def 'flags a regression against history'() {
        given: 'three prior runs where CRUNCH took ~2s'
        def prior = (1..3).collect {
            run('toy', [CRUNCH: [tasks: 1, failed: 0, realtime_ms_median: 2000]])
        }

        when: 'the current run takes 6s'
        def findings = InsightEngine.compare(
            [CRUNCH: [tasks: 1, failed: 0, realtime_ms_median: 6000]], prior)

        then:
        findings.size() == 1
        findings[0].type == 'regression'
        findings[0].severity == 'warn'
        findings[0].message.contains('CRUNCH')
        findings[0].message.contains('3.0x slower')
    }

    def 'small absolute changes are never flagged even at high ratio'() {
        given: 'history of 100ms, 3x slower but only 200ms absolute'
        def prior = [run('toy', [QUICK: [tasks: 1, failed: 0, realtime_ms_median: 100]])]

        when:
        def findings = InsightEngine.compare(
            [QUICK: [tasks: 1, failed: 0, realtime_ms_median: 300]], prior)

        then:
        findings.isEmpty()
    }

    def 'flags improvement when a process gets much faster'() {
        given:
        def prior = [run('toy', [SLOW: [tasks: 1, failed: 0, realtime_ms_median: 10_000]])]

        when:
        def findings = InsightEngine.compare(
            [SLOW: [tasks: 1, failed: 0, realtime_ms_median: 3000]], prior)

        then:
        findings.size() == 1
        findings[0].type == 'improvement'
        findings[0].severity == 'info'
    }

    def 'flags over-provisioned memory across all samples'() {
        given: 'requested 8 GB, never used more than 500 MB in any run'
        long gb8 = 8L * 1024 * 1024 * 1024
        long mb500 = 500L * 1024 * 1024
        def prior = [run('toy', [BIG: [tasks: 1, failed: 0, realtime_ms_median: 5000, peak_rss_max: mb500]])]

        when:
        def findings = InsightEngine.compare(
            [BIG: [tasks: 1, failed: 0, realtime_ms_median: 5000, peak_rss_max: mb500, memory_req: gb8]], prior)

        then:
        findings.size() == 1
        findings[0].type == 'overprovision'
        findings[0].message.contains('BIG')
    }

    def 'does not flag over-provisioning when memory is actually used'() {
        given: 'requested 1 GB, used 800 MB'
        long gb1 = 1024L * 1024 * 1024
        long mb800 = 800L * 1024 * 1024

        when:
        def findings = InsightEngine.compare(
            [OK: [tasks: 1, failed: 0, realtime_ms_median: 5000, peak_rss_max: mb800, memory_req: gb1]], [])

        then:
        findings.isEmpty()
    }

    def 'flags failed tasks'() {
        when:
        def findings = InsightEngine.compare(
            InsightEngine.aggregate([task('A', 1000), task('A', 1000, null, null, 'FAILED')]), [])

        then:
        findings.size() == 1
        findings[0].type == 'failures'
        findings[0].message.contains('1 of 2')
    }

    def 'renders a report with baseline note on first run'() {
        given:
        def runRec = [pipeline: 'toy', run_name: 'boring_euler', ts: '2026-08-17T12:00:00Z',
                      processes: InsightEngine.aggregate([task('A', 2000)])]

        when:
        def report = InsightEngine.renderReport(runRec, [], [])

        then:
        report.contains('# nf-runinsights report')
        report.contains('baseline established')
        report.contains('| A | 1 | 2.0s |')
    }

    def 'attributes a regression to queue wait when waiting explains the delta'() {
        given: 'history: 2s runtime with negligible queue; current: 8s runtime, 5s of it queued'
        def prior = [run('toy', [ALIGN: [tasks: 1, failed: 0, realtime_ms_median: 2000, queue_ms_median: 100]])]

        when:
        def findings = InsightEngine.compare(
            [ALIGN: [tasks: 1, failed: 0, realtime_ms_median: 8000, queue_ms_median: 5100]], prior)

        then:
        findings.size() == 1
        findings[0].type == 'regression'
        findings[0].message.contains('queue wait')
    }

    def 'attributes a regression to a container change'() {
        given:
        def prior = [run('toy', [CALL: [tasks: 1, failed: 0, realtime_ms_median: 2000, container: 'gatk:4.2.0']])]

        when:
        def findings = InsightEngine.compare(
            [CALL: [tasks: 1, failed: 0, realtime_ms_median: 6000, container: 'gatk:4.3.0']], prior)

        then:
        findings[0].type == 'regression'
        findings[0].message.contains('container changed (gatk:4.2.0 → gatk:4.3.0)')
    }

    def 'attributes a slowdown to input growth when bytes grew with time'() {
        given: '3x the data taking 3x the time is growth, not regression'
        def prior = [run('toy', [SORT: [tasks: 1, failed: 0, realtime_ms_median: 3000, read_bytes_total: 1_000_000]])]

        when:
        def findings = InsightEngine.compare(
            [SORT: [tasks: 1, failed: 0, realtime_ms_median: 9000, read_bytes_total: 3_000_000]], prior)

        then:
        findings[0].type == 'regression'
        findings[0].message.contains('input data grew 3.0x')
    }

    def 'regressions without causal evidence carry no hint'() {
        given: 'old-format history with none of the causal fields'
        def prior = [run('toy', [X: [tasks: 1, failed: 0, realtime_ms_median: 2000]])]

        when:
        def findings = InsightEngine.compare(
            [X: [tasks: 1, failed: 0, realtime_ms_median: 8000]], prior)

        then:
        findings[0].type == 'regression'
        !findings[0].message.contains('likely cause')
    }

    def 'detects a pipeline-wide environmental slowdown'() {
        given: 'five processes all ~1.4x slower, none crosses its own threshold'
        def procsBase = (1..5).collectEntries { ["P$it".toString(), [tasks: 1, failed: 0, realtime_ms_median: 3000]] }
        def procsCur  = (1..5).collectEntries { ["P$it".toString(), [tasks: 1, failed: 0, realtime_ms_median: 4200]] }
        def prior = [run('toy', procsBase)]

        when:
        def findings = InsightEngine.compare(procsCur, prior)

        then: 'one environment finding, no per-process regression noise'
        findings.size() == 1
        findings[0].type == 'environment'
        findings[0].message.contains('5 of 5')
    }

    def 'flags retried tasks'() {
        when:
        def findings = InsightEngine.compare(
            [FLAKY: [tasks: 4, failed: 0, retried: 2, realtime_ms_median: 1000]], [])

        then:
        findings.size() == 1
        findings[0].type == 'retries'
        findings[0].message.contains('2 of 4')
    }

    def 'aggregate computes queue, efficiency, io, retries and container'() {
        when:
        def agg = InsightEngine.aggregate([
            [process: 'A', status: 'COMPLETED', realtime_ms: 4000, cpus: 2, pcpu: 150.0,
             queue_ms: 500, attempt: 1, container: 'tool:1.0', read_bytes: 1000],
            [process: 'A', status: 'COMPLETED', realtime_ms: 6000, cpus: 2, pcpu: 50.0,
             queue_ms: 1500, attempt: 2, container: 'tool:1.0', read_bytes: 3000],
        ])

        then:
        agg.A.queue_ms_median == 1000
        agg.A.cpu_eff_median == 0.5          // (0.75 + 0.25) / 2
        agg.A.read_bytes_total == 4000
        agg.A.retried == 1
        agg.A.container == 'tool:1.0'
    }

    def 'median handles even and odd counts and nulls'() {
        expect:
        InsightEngine.median([1, 2, 3]) == 2
        InsightEngine.median([1, 2, 3, 4]) == 2.5
        InsightEngine.median([5, null]) == 5
        InsightEngine.median([]) == null
        InsightEngine.median(null) == null
    }
}
