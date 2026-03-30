# KEP-NNNN: Your short, descriptive title

<!--
This is the title of your KEP. Keep it short, simple, and descriptive. A good
title can help communicate what the KEP is and should be considered as part of
any review.
-->

<!--
A table of contents is helpful for quickly jumping to sections of a KEP and for
highlighting any additional information provided beyond the standard KEP
template.

Ensure the TOC is wrapped with
  <code>&lt;!-- toc --&rt;&lt;!-- /toc --&rt;</code>
tags, and then generate with `hack/update-toc.sh`.
-->

<!-- toc -->
- [Summary](#summary)
- [Motivation](#motivation)
  - [Goals](#goals)
  - [Non-Goals](#non-goals)
- [Proposal](#proposal)
  - [User Stories](#user-stories)
    - [Story 1](#story-1)
    - [Story 2](#story-2)
  - [Notes/Constraints/Caveats (Optional)](#notesconstraintscaveats)
  - [Risks and Mitigations](#risks-and-mitigations)
- [Design Details](#design-details)
  - [Test Plan](#test-plan)
    <!-- - [Prerequisite testing updates](#prerequisite-testing-updates) -->
    - [Unit tests](#unit-tests)
    - [Integration tests](#integration-tests)
    - [e2e tests](#e2e-tests)
  - [Graduation Criteria](#graduation-criteria)
- [Implementation History](#implementation-history)
- [Drawbacks](#drawbacks)
- [Alternatives](#alternatives)
<!-- /toc -->

## Summary

The Workload status in queue in the Management Cluster must reflect the true state of the workload derived from the remote cluster,
including a human-readable message explaining the state (e.g., "Waiting for quota in cluster X").

This KEP focuses on a mechanism to provide such high-level summary in the form of a new Workload Status Condition - the **MultiKueueWorkload** conditon.
The condition:
1. Will be populated for workloads created on the Manager Cluster.
1. Will provide a high-level, human-readable message explaining the state of the Manager Workload.
1. Will provide insights into the Workload's progress throughout its **whole lifecycle**.
1. Will aggregate the information on all the Remote Workloads dispatched by MultiKueue to Worker Clusters for the subject Manager Workload.

## Motivation

Currently, the Workload Status subresource is missing key information aggregating data from across its remote counterparts on Worker Clusters.
The existing condition do not track the process of dispatching Remote Workloads to Workers, obstructing the details of MultiKueue's most critical part of the workloads execution.
This forces users to search accross all Worker Clusters to be able to see the big picture.

Moreover, it does not natively support a contract defining a human readable execution status.
It instead relies on a list of Conditions and support information provided inside the WorkloadStatus field for the user to piece the actual global state of the underlying job's execution totehter on their own.

To amend this we need a way to present the user with a clearly defined, user-readable summary of the Global State, which aggregates information from across all the clusters of the MultiKueue environment.

If nothing is implemented, the users will remain forced to rely on:
* the conditions of the Manager Workload, which are misaligned and non-representative of all the extensive logic happening in MultiKueue,
* manually querying and aggregating the conditions of Remote Workload across all Registered Workers; this covers a lot of distributed data; users are missing the core information of which Workers are eligible for distribution by virtue of being put forward by the Distribution Strategy.

### Goals

* Adding a new Workload Status Condition describing the current, actual state of the Manager Workload.
* Defining an enumeration of mutually exclusive MultiKueue Manager Workload states covering the whole lifecycle of the workload.
* Defining a set of high-level, human readable messages to accompany the enumerated states, describing them further.
* Populating the condition for relevant workloads.

### Non-Goals

* Defining Status Summary describing data structures beyond whats necessary for implementing the new Workload Status Condition.

## Proposal

The lifecycle of the Manager Workload will be split into the following **MultiKueueGlobalStatus** enumeration:
* **SUCCESS** - workload finished successfully,
* **FAILED** - workload finished with failed state,
* **INACTIVE** - Manager Workload marked as inactive, preventing it from being scheduled; we can enter this state from any other as the workload can be deactivated both by Kueue and by the user,
* **RUNNING** - local workload is admitted (has the admitted condition); a worker was nominated; the remote is admitted (has the admitted condition); the underlying job will attempt to execute; if it finishes we transition into the SUCCESS/FAILED state, otherwise we back-off,
* **WORKER_SELECTED** - local workload is admitted (has the admitted condition); a single worker was nominated; the remote has currently received quota but is not admitted yet;
this is possible if the MultiKueueWaitForWorkloadAdmitted feature is disabled; it can also occur as a consequence of graduating from the WAITING_FOR_WORKER state when the behavior described in  #9338  occurs,
* **WAITING_FOR_WORKER** -  quota reserved on the local workload; dispatching remotes to eligible workers and waiting  to nominate one of them; a worker will be nominated once the remote achieves a state allowing it to graduate to either the WORKER_SELECTED or the RUNNING state,
Note: Local workload can be admitted (have the admitted condition), as described in #9338 (when MultiKueue admits a workload on a worker but then it gets preempted there with the MultiKueueRedoAdmissionOnEvictionInWorker gate enabled).
* **WAITING_FOR_WORKER_NOMINATION** - specific to a non-primary component workload in the multi-workload-resource handling scenario;  quota reserved on the component workload; the component workload is waiting for the primary to nominate a worker to create a remote on,
* **WAITING_FOR_QUOTA** - local workload is waiting to be granted a quota reservation on the Manager Cluster.

For each MultiKueueGlobalStatus a message - **MultiKueueGlobalStatusMessage** - will be defined:
* SUCCESS: `Workload has finished successfully on worker cluster: <worker cluster reference>.`
* FAILED: `Workload failed after admission on worker cluster: <worker cluster reference>.`
* INACTIVE: `Workload inactive: <reason>.`
* RUNNING: `Workload admitted on worker cluster: <worker cluster reference>.`
* WORKER_SELECTED: `Workload received quota reservation on worker cluster: <worker cluster reference>. <number of ready admission checks>/<number of all admission checks> Admission Checks Ready.`
* WAITING_FOR_WORKER:
  * Default:
  `Workload awaiting admission on one of the registered workers. <number of remotes> remotes created.`
  * Non primary component workload:
  `Component Workload awaiting admission on worker cluster: <worker nominated by the primary local workload>, nominated by <primary local workload reference>.`
* WAITING_FOR_WORKER_NOMINATION: `Component Workload waiting for <primary local workload reference> to nominate a worker cluster.`
* WAITING_FOR_QUOTA: `Workload awaiting quota on the manager cluster.`

The **MultiKueueWorkload** condition will be defined as:
|Field|Value|
|---|---|
|Type|_MultiKueueWorkload_|
|Status|_True_|
|Reason|MultiKueueGlobalStatus|
|Message|MultiKueueGlobalStatusMessage|

### User Stories

#### Story 1

I want to get an idea of when my job will be admitted. Has it already received quota on the manager?
Is it being dispatched? If yes - what are the workers has it been dispatched to, so that I can check the status of some or all of them for how my job is doing there?
Or maybe it is already running on a specific worker? If yes - which one?

#### Story 2

My job hasn’t been admitted for a long time. I would like to verify why that is.
Are remotes being created? Have they been dispatched to all workers? What is their status there? Are they waiting for quota? Are they waiting for admission checks?

### Notes/Constraints/Caveats

This proposal is limited to only providing the user with a high-level summary.
This is a minimum value proposition, lacking in a few key areas:
* In the WAITING_FOR_WORKER state we only provide a miniscule summary of what is happening on the Workers. In reality, this state is much more complex than the messages we propose can describe and could greatly benefiot form being accompanied by a set of aggregations describing in greater detail what is the status of each Remote Workload.
* In an effort ot keep the condition message concise, we ware limited in how much data relevant to the user we can provide.

In the [alternatives section](#global-status-summary) we breifly describe an expanded Global Status Summary proposal to be revisited in the future if the new conidtion by itself is deemed insufficient.

### Risks and Mitigations

<!--
What are the risks of this proposal, and how do we mitigate? Think broadly.
For example, consider both security and how this will impact the larger
Kubernetes ecosystem.

How will security be reviewed, and by whom?

How will UX be reviewed, and by whom?

Consider including folks who also work outside the SIG or subproject.
-->

## Design Details

The new condition type will be defined alongside existing ones in the [workload types file](https://github.com/Singularity23x0/kueue/blob/66c2cf1a93ee3f19d8b29309d27832a4595d4106/apis/kueue/v1beta1/workload_types.go#L628).

```go
const (
  // ...

  // MultiKueueWorkload mean the workload is a MultiKueue Workload created on a Manager Cluster.
  // The possible reasons depend on the state of the MK Workload:
  // - SUCCESS,
  // - FAILED,
  // - INACTIVE,
  // - RUNNING,
  // - WORKER_SELECTED,
  // - WAITING_FOR_WORKER,
  // - WAITING_FOR_WORKER_NOMINATION,
  // - WAITING_FOR_QUOTA.
  MultiKueueWorkload = "MultiKueueWorkload"

  // ...
)
```

The MultiKueueGlobalStatus enumeration will be defined in the [MultiKueue types file](https://github.com/Singularity23x0/kueue/blob/66c2cf1a93ee3f19d8b29309d27832a4595d4106/apis/kueue/v1beta1/multikueue_types.go#L17).

```go
const (
  // Success state means the workload has finished successfully.
  Success = "SUCCESS"

  // Failed state means the workload has finished with the Failed state.
  Failed = "FAILED"

  // Inactive state means the workload is inactive.
  Inactive = "INACTIVE"

  // Running state means the workload has the "Admitted" condition on the Manager Cluster and was admitted on a specific Worker Cluster.
  // The underlying job is being executed on said Worker Cluster.
  Running = "RUNNING"

  // WorkerSelected state means the workload has the "Admitted" condition on the Manager Cluster but was not admitted on the Worker yet.
  // A specific Worker was nominated, but the workload has only managed to reserve quota there so far.
  WorkerSelected = "WORKER_SELECTED"

  // WaitingForWorker state means the workload has received quota on the Manager Cluster.
  // MultiKueue is currently dispatching remote workloads to eligible Workers.
  WaitingForWorker = "WAITING_FOR_WORKER"

  // WaitingForWorkerNomination state is specific to a non-primary component workload in the multi-workload-resource handling scenario.
  // It means the component workload has received quota reservation on the Manager
  // and is waiting for the primary component workload to nominate a worker to dispatch a remote to.
  WaitingForWorkerNomination = "WAITING_FOR_WORKER_NOMINATION"

  // WaitingForQuota state means the workload is currently in the "Pending" state on the Manager Cluster (does not currently hold any quota reservations).
  WaitingForQuota = "WAITING_FOR_QUOTA"
)

```

The condition will be populated inside the [Reconciler of the MultiKueue Core Controller](https://github.com/Singularity23x0/kueue/blob/66c2cf1a93ee3f19d8b29309d27832a4595d4106/pkg/controller/admissionchecks/multikueue/workload.go#L158).
This reconiler already gathers all the necessary data in the form of the [Workload Group](https://github.com/Singularity23x0/kueue/blob/66c2cf1a93ee3f19d8b29309d27832a4595d4106/pkg/controller/admissionchecks/multikueue/workload.go#L83) internal structure.

The logic identifying the MultiKueueGlobalStatus,
calculating neccessary aggregations and generating the appropriate MultiKueueGlobalStatusMessage will be implemented in the
MultiKueue Core Controller as well, since that is where the logic will be used.

The exact MultiKueueGlobalStatus to assign can be determined using the conditions in the Manager Workload and the conditions in each of the Remote Workloads, all already gathered as part of the reconciliation process.

For the defined messages, each variable can be determined using the data present during the reconciliation process:
* _worker cluster reference_ of the nominated Worker is retrieved as part of processing (when in one of {SUCCESS, FAILED, RUNNING, WORKER_SELECTED} states),
* the _reason_ for why the Manager Workload is inactive is present in an appropriate condition of the Manager Workload,
* _number of remotes_ is number of the noted remotes,
* _worker nominated by the primary local workload_ and _primary local workload reference_ are already identified in the curren reconciliation process.

### Test Plan

[ ] I/we understand the owners of the involved components may require updates to
existing tests to make this code solid enough prior to committing the changes necessary
to implement this enhancement.

<!--
#### Prerequisite testing updates

Based on reviewers feedback describe what additional tests need to be added prior
implementing this enhancement to ensure the enhancements have also solid foundations.
-->

#### Unit tests

Unit tests will be added for the methods identifying the MultiKueueGlobalStatus and calculating the condition as part of the [MultiKueue Workload Controller](https://github.com/Singularity23x0/kueue/blob/66c2cf1a93ee3f19d8b29309d27832a4595d4106/pkg/controller/admissionchecks/multikueue/workload_test.go#L17) test suite.

#### Integration tests

Integration tests for assigning the expected condition correctly for each Global Status will be added in a new file as part of the [MultiKueue Integration Test Suite](https://github.com/Singularity23x0/kueue/blob/66c2cf1a93ee3f19d8b29309d27832a4595d4106/test/integration/multikueue/suite_test.go#L93).

#### e2e tests

Integration tests for assigning the expected condition correctly for each Global Status will be added in a new file as part of the [MultiKueue E2E Test Suite](https://github.com/Singularity23x0/kueue/blob/66c2cf1a93ee3f19d8b29309d27832a4595d4106/test/e2e/multikueue/suite_test.go#L62).

### Graduation Criteria

<!--

Clearly define what it means for the feature to be implemented and
considered stable.

If the feature you are introducing has high complexity, consider adding graduation
milestones with these graduation criteria:
- [Maturity levels (`alpha`, `beta`, `stable`)][maturity-levels]
- [Feature gate][feature gate] lifecycle
- [Deprecation policy][deprecation-policy]

[feature gate]: https://git.k8s.io/community/contributors/devel/sig-architecture/feature-gates.md
[maturity-levels]: https://git.k8s.io/community/contributors/devel/sig-architecture/api_changes.md#alpha-beta-and-stable-versions
[deprecation-policy]: https://kubernetes.io/docs/reference/using-api/deprecation-policy/
-->

- **Alpha**:
- **Beta**:
- **Stable**:


## Implementation History

- **2026-03-30**: initial KEP draft.

## Drawbacks

- We add a Condition that does not behave as a typical one would:
  - it is expected to either be True or not present,
  - the reason is not really a reason in the semantic sense of the word.
-

## Alternatives

### Global Status Summary


