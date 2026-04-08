# KEP-10189: MultiKueue Global Status Condition

<!-- toc -->
- [Summary](#summary)
- [Motivation](#motivation)
  - [Goals](#goals)
  - [Non-Goals](#non-goals)
- [Proposal](#proposal)
  - [User Stories](#user-stories)
    - [Story 1](#story-1)
    - [Story 2](#story-2)
  - [Notes/Constraints/Caveats](#notesconstraintscaveats)
  - [Risks and Mitigations](#risks-and-mitigations)
- [Design Details](#design-details)
  - [Test Plan](#test-plan)
    - [Unit tests](#unit-tests)
    - [Integration tests](#integration-tests)
    - [e2e tests](#e2e-tests)
  - [Graduation Criteria](#graduation-criteria)
- [Implementation History](#implementation-history)
- [Drawbacks](#drawbacks)
- [Alternatives](#alternatives)
  - [Global Status Summary](#global-status-summary)
  - [Visibility API](#visibility-api)
  - [Consolidated Workload State](#consolidated-workload-state)
<!-- /toc -->

## Summary

The Workload status in the MultiKueue Manager Cluster must reflect the true state of the workload derived from the Worker Clusters,
including a human-readable message explaining the state (e.g., "Waiting for quota in cluster X").

This KEP focuses on a mechanism to provide such high-level summary in the form of a new Workload Status Condition - the **MultiKueueWorkload** condition.
The condition:
1. Will be populated for workloads created on the Manager Cluster.
1. Will provide a high-level, human-readable message explaining the state of the Manager Workload.
1. Will provide insights into the Workload's progress throughout its **whole lifecycle**.
1. Will aggregate the information from all the Remote Workloads dispatched by MultiKueue to Worker Clusters for the subject Manager Workload.

## Motivation

Currently, the Workload Status subresource is missing key information aggregating data from across its remote counterparts on Worker Clusters.
The existing conditions do not track the process of dispatching Remote Workloads to Workers, obstructing the details of MultiKueue's most critical part of the workload's execution.
This forces users to search across all Worker Clusters to be able to see the big picture.

Moreover, it does not natively support a contract defining a human-readable execution status.
It instead relies on a list of Conditions and support information provided inside the WorkloadStatus field for the user to piece the actual global state of the underlying job's execution together on their own.

To amend this, we need a way to present the user with a clearly defined, user-readable summary of the Global State, which aggregates information from across all the clusters of the MultiKueue environment.

If nothing is implemented, the users will remain forced to rely on:
* the conditions of the Manager Workload, which are misaligned and non-representative of all the extensive logic happening in MultiKueue,
* manually querying and aggregating the conditions of Remote Workload across all Registered Workers; this covers a lot of distributed data; users are missing the core information of which Workers are eligible for distribution by virtue of being put forward by the Distribution Strategy.

### Goals

* Adding a new Workload Status Condition describing the current, actual state of the Manager Workload.
* Defining an enumeration of mutually exclusive MultiKueue Manager Workload states covering the whole lifecycle of the workload.
* Defining a set of high-level, human-readable messages to accompany the enumerated states, describing them further.
* Populating the condition for relevant workloads.

### Non-Goals

* Defining Status Summary describing data structures beyond what's necessary for implementing the new Workload Status Condition.

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

The condition will be gated behind the **MultiKueueGlobalStatusCondition** feature gate.

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

In the [alternatives section](#global-status-summary) we briefly describe an expanded Global Status Summary proposal to be revisited in the future if the new condition by itself is deemed insufficient.

### Risks and Mitigations

The main risk of the feature is its effect on the MultiKueue Reconciler's performance and code complexity.
Implementing the condition calculating logic efficiently will require careful consideration of where the Reconciler can reliably determine the state of the Manager Workload.
That may necessitate distributing pieces of logic across functions, complicating the already convoluted logic. Mitigating that will require careful naming and code grouping in the implementation phase.

Another issue stems from the MultiKueue's complex logic itself. It may prove difficult to reliably track the actual state due to the branching nature of the functions.
Countering that will require thorough test coverage, with a special emphasis on implementing a wide coverage in E2E tests.

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
This reconciler already gathers all the necessary data in the form of the [Workload Group](https://github.com/Singularity23x0/kueue/blob/66c2cf1a93ee3f19d8b29309d27832a4595d4106/pkg/controller/admissionchecks/multikueue/workload.go#L83) internal structure.

The logic identifying the MultiKueueGlobalStatus,
calculating necessary aggregations and generating the appropriate MultiKueueGlobalStatusMessage will be implemented in the
MultiKueue Core Controller as well, since that is where the logic will be used.

The exact MultiKueueGlobalStatus to assign can be determined using the conditions in the Manager Workload and the conditions in each of the Remote Workloads, all already gathered as part of the reconciliation process.

For the defined messages, each variable can be determined using the data present during the reconciliation process:
* _worker cluster reference_ of the nominated Worker is retrieved as part of processing (when in one of {SUCCESS, FAILED, RUNNING, WORKER_SELECTED} states),
* the _reason_ for why the Manager Workload is inactive is present in an appropriate condition of the Manager Workload,
* _number of remotes_ is number of the noted remotes,
* _worker nominated by the primary local workload_ and _primary local workload reference_ are already identified in the current reconciliation process.

### Test Plan

[ ] I/we understand the owners of the involved components may require updates to
existing tests to make this code solid enough prior to committing the changes necessary
to implement this enhancement.

#### Unit tests

Unit tests will be added for the methods identifying the MultiKueueGlobalStatus and calculating the condition as part of the [MultiKueue Workload Controller](https://github.com/Singularity23x0/kueue/blob/66c2cf1a93ee3f19d8b29309d27832a4595d4106/pkg/controller/admissionchecks/multikueue/workload_test.go#L17) test suite.

#### Integration tests

Integration tests for assigning the expected condition correctly for each Global Status will be added in a new file as part of the [MultiKueue Integration Test Suite](https://github.com/Singularity23x0/kueue/blob/66c2cf1a93ee3f19d8b29309d27832a4595d4106/test/integration/multikueue/suite_test.go#L93).

#### e2e tests

Integration tests for assigning the expected condition correctly for each Global Status will be added in a new file as part of the [MultiKueue E2E Test Suite](https://github.com/Singularity23x0/kueue/blob/66c2cf1a93ee3f19d8b29309d27832a4595d4106/test/e2e/multikueue/suite_test.go#L62).

### Graduation Criteria

- **Alpha**:
  - Feature is implemented behind the **MultiKueueGlobalStatusCondition** feature gate. FG disabled by default.
  - Unit, integration and E2E tests are implemented and confirmed passing and non-flaky.
- **Beta**:
  - Feature Gate is enabled by default.
  - The condition is confirmed (using a production-like environment) to populate correctly and reflect the actual state of the Manager Workload.
  - User feedback is gathered and taken into consideration.
- **Stable**:
  - The condition is populated as expected, as confirmed by tests and users.
  - Feature gate is removed.
  - Feature is confirmed as stable.

## Implementation History

- **2026-04-01**: initial KEP draft.

## Drawbacks

- We add a Condition that does not behave as a typical one would:
  - it is expected to either be True or not present,
  - the Condition Reason is not really a reason, but rather a Manager Workload state identifier.
- The information provided remains minimal. The users are still missing any substantial details on what is happening with the underlying architecture as the Workload enters the **WAITING_FOR_WORKER** state.

## Alternatives

### Global Status Summary

We define a new resource - **GlobalStatusSummary** - to be created on the Manager Cluster alongside the Manager Workload.
Aside from the **GlobalStatus** and the **GlobalStatusMessage** it would also contain the following:
- WorkloadReference of the related Manager Workload,
- AdditionalMessages - additional messages describing the state; a list with contents depending on which type of state is assigned,
- Admission - a field containing the data on the nominated Worker and Remote when in WORKER_SELECTED/RUNNING state. Empty in any other state,
- AdmissionHistory - a list of entries representing Workers which historically were nominated for this local workload but have failed to reach the Finished state. A remote is moved here from the Admission field when a backoff occurs,
- WorkerDetails - a field containing a detailed description of the state of the workers and their remotes in the form of high-level aggregations,
- MultiWorkloadSpec - a specification of the multi-workload setup if local workload is part of one, otherwise empty.

The resource would be calculated in the same place as the proposed Condition and created as an object accompanying the Manager Workload throughout its lifetime.

This approach can be considered as a natural extension of the one proposed in the KEP and will be revisited in the future if need arises.

### Visibility API

Instead of persisting the data in the Manager Cluster and calculating it in the MultiKueue Controller, we instead provide it on demand via the Visibility API.

This approach is ill advised as the data is expected to be stable and using the Visibility API is not necessary.

### Consolidated Workload State

Instead of defining the proposed enumeration of MultiKueue Workload States, we instead provide a unified set of States to describe workloads regardless of context.

The proposed set of states would be as follows:

| Common state | Meaning when applied to a manager workload | Meaning for other workloads | Corresponding global state | Corresponding (generic/individual) workload state |
| :--- | :--- | :--- | :--- | :--- |
| SUCCESS | Underlying job executed successfully. Local workload has the finished condition with “Succeeded” reason. The remote is in the SUCCESS (Finished) state. | The underlying job executed successfully. Workload has the finished condition with “Succeeded” reason. | SUCCESS | Finished |
| FAILED | A worker was nominated, a remote admitted and the job executed. The job failed on the nominated worker. Local workload has the finished condition with “Failed” reason. The remote is in the FAILED (Finished) state. | The underlying job was executed and failed. Workload has the finished condition with “Failed” reason. | FAILED | Finished |
| INACTIVE | Local workload inactive. No remotes exist. | Workload is inactive. | INACTIVE | Inactive |
| ADMITTED | Local workload has the admitted condition. Worker nominated and remote in state ADMITTED (Admitted). Worker is attempting to execute the job. | Workload has an admission and the admitted condition. The cluster is attempting to execute the underlying job. | RUNNING | Admitted |
| ADMISSION PENDING | Local workload has the admitted condition. Worker nominated and remote in state ADMISSION PENDING (QuotaReserved). | Workload has an admission but does not have the admitted condition. This means the workload received a quota reservation but not all admission checks have reached the Ready state yet. | WORKER_SELECTED | QuotaReserved |
| WAITING FOR WORKER | Local has the admission but not the admitted condition. Dispatching remotes to one or more workers. All remotes are PENDING (Pending) or ADMISSION PENDING (QuotaReserved). | — | WAITING_FOR_WORKER | — |
| WAITING FOR WORKER NOMINATION | Local workload is a non-primary workload in a group of composite workloads. Local has got an admission but not the admitted condition. Primary has not nominated a worker yet. | — | WAITING_FOR_WORKER_NOMINATION | — |
| PENDING | Local workload does not have an admission. No remotes exist. | Workload does not have an admission. | WAITING_FOR_QUOTA | Pending |

This approach risks being confusing to users, as the Workload States shift meanings significantly depending on whether the Workload is a Manager Workload or an Individual (remote or non-multikueue) Workload.
