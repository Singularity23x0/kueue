# KEP-11823: EffectiveResourceGroups

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
  - [User Stories (Optional)](#user-stories-optional)
    - [Story 1 (Optional)](#story-1-optional)
    - [Story 2 (Optional)](#story-2-optional)
  - [Notes/Constraints/Caveats (Optional)](#notesconstraintscaveats-optional)
  - [Risks and Mitigations](#risks-and-mitigations)
- [Design Details](#design-details)
  - [Test Plan](#test-plan)
    - [Prerequisite testing updates](#prerequisite-testing-updates)
    - [Unit tests](#unit-tests)
    - [Integration tests](#integration-tests)
    - [e2e tests](#e2e-tests)
  - [Graduation Criteria](#graduation-criteria)
- [Implementation History](#implementation-history)
- [Drawbacks](#drawbacks)
- [Alternatives](#alternatives)
<!-- /toc -->

## Summary

<!--
This section is incredibly important for producing high-quality, user-focused
documentation such as release notes or a development roadmap. It should be
possible to collect this information before implementation begins, in order to
avoid requiring implementors to split their attention between writing release
notes and implementing the feature itself. KEP editors and SIG Docs
should help to ensure that the tone and content of the `Summary` section is
useful for a wide audience.

A good summary is probably at least a paragraph in length.

Both in this section and below, follow the guidelines of the [documentation
style guide]. In particular, wrap lines to a reasonable length, to make it
easier for reviewers to cite specific portions, and to minimize diff churn on
updates.

[documentation style guide]: https://github.com/kubernetes/community/blob/master/contributors/guide/style-guide.md
-->

This KEP extends the current resource group definition mechanism of Kueue to accomodate automated quota management.

The proposal is to introduce a new ClusterQueue Status field: EffectiveResourceGroups, used to represent and track the ClusterQueue quota as interpreted by the Kueue scheduler.

## Motivation

<!--
This section is for explicitly listing the motivation, goals, and non-goals of
this KEP.  Describe why the change is important and the benefits to users. The
motivation section can optionally provide links to [experience reports] to
demonstrate the interest in a KEP within the wider Kubernetes community.

[experience reports]: https://github.com/golang/go/wiki/ExperienceReports
-->

We need a way to track the ClusterQueue quota. In the default case the quota is static and described by the user in spec.ResourceGroups.

When automating effective quota calculation, using spec.ResourceGroups to store the value would mean allowing Kueue to modify spec of objects it's supposed to reconcile (specifically, ClusterQueues). This may be perceived as a design smell because:
* It blurs the line between configuration (managed by users) and state (managed by controllers).
* It brings a risk of an infinite reconciling loop.

To reslove this issue, we propose introducing a new ClusterQueue Status field: EffectiveResourceGroups to store the automated quota value.


### Goals

<!--
List the specific goals of the KEP. What is it trying to achieve? How will we
know that this has succeeded?
-->
1. Implement the EffectiveResourceGroups status field in ClusterQueue API.
2. Define a clear contract on how EffectiveResourceGroups is managed and utilized by Kueue in cases of:
    * Default setup: MultiKueue disabled; this case will be the basis for any possible future additions of more automated ClusterQueue quota management schemes,
    * MultiKueue setup without Quota automation: MultiKueue enabled, MultiKueueManagerQuotaAutomation feature disabled.
    * MultiKueue with MultiKueueManagerQuotaAutomation enabled.

### Non-Goals

<!--
What is out of scope for this KEP? Listing non-goals helps to focus discussion
and make progress.
-->

## Proposal

<!--
This is where we get down to the specifics of what the proposal actually is.
This should have enough detail that reviewers can understand exactly what
you're proposing, but should not include things like API designs or
implementation. What is the desired outcome and how do we measure success?.
The "Design Details" section below is for the real
nitty-gritty.
-->

We introduce the new status.EffectiveResourceGroups field alongside the spec.ResourceGroups field.
The status.EffectiveResourceGroups will add a layer between the spec.ResourceGroups (set diretcly by users) and Kueue logic. All internal Kueue logic will use status.EffectiveResourceGroups as the source of truth for resource quota. Setting the value of the effective quota will be handled by the Core-ClusterQueue-Controller and MultiKueue-ClusterQueue-Controller depending on the configuration:

1. By default, EffectiveResourceGroups will always mirror spec.ResourceGroups. EffectiveResourceGroups will be synced to spec.ResourceGroups during reconcilition in the Core ClusterQueue Controller.
2. When MultiQueue Automated Quota Management is **enabled** and **configured correctly**, effective quta will be calculated by the MultiKueue ClusterQueue controller. The value will be set directly to one representing the aggregated quota accross the Manager Queue's Workers, calculated as decided upon in <!--- TODO: Add issue link--->.

We consider MultiQueue Automated Quota Management **enabled** and **configured correctly** when:
1. feature.MultiKueue is enabled.
2. feature.MultiKueueManagerQuotaAutomation is enabled.
3. The ClusterQueue is MultiKueue, i.e. it has the MultiKueue Admission Check.
4. The Quota Management Strategy is present (non-nill) and set to **"Automated"**.
5. **spec.ResourceGroup is empty.**

We expect spec.ResourceGroup to be empty for MultiKueue Quota Automation to avoid user confusion. If we did allow spec.ResourceGroup to be set while at the same time enabling MultiKueue Quota Automation, it wouldn't be clear for the user which value is taken into account when admitting workloads:
1. If we simply ignore spec.ResourceGroup, users would be confused as to why the quota they set manually is ignored by MultiKueue.
2. If we modified spec.ResourceGroup in any way, it would counter the main goal of introducing this change in the first place.

Instead, the users are expected to set spec.ResourceGroup to empty and will be notified of any misconfigurations via the ClusterQueue's Conditions.

### User Stories (Optional)

<!--
Detail the things that people will be able to do if this KEP is implemented.
Include as much detail as possible so that people can understand the "how" of
the system. The goal here is to make this feel real for users without getting
bogged down.
-->

#### Story 1 (Optional)

#### Story 2 (Optional)

### Notes/Constraints/Caveats (Optional)

<!--
What are the caveats to the proposal?
What are some important details that didn't come across above?
Go in to as much detail as necessary here.
This might be a good place to talk about core concepts and how they relate.
-->

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

status.EffectiveResourceGroups will be managed by either the Core ClusterQueue Controller or the MultiKueue ClusterQueue Controller (mutually exlusive):
1. status.EffectiveResourceGroups will be managed by the MultiKueue Controller when:
    * features.MultiKueue is enabled AND
    * features.MultiKueueManagerQuotaAutomation is enabled AND
    * ClusterQueue has the MultiKueue admission check assigned.
2. Otherwise, status.EffectiveResourceGroups will be managed by the Core Controller.

**Default Behavior**: By default status.EffectiveResourceGroups will be synced to the current value spec.ResourceGroups.

```go
// pkg/util/queue/cluster_queue.go

func SyncEffectiveResourceGroupToSpec(cq *kueue.ClusterQueue) (needsUpdate bool) {
	if needsUpdate = !equality.Semantic.DeepEqual(cq.Status.EffectiveResourceGroups, cq.Spec.ResourceGroups); needsUpdate {
		cq.Status.EffectiveResourceGroups = cq.Spec.ResourceGroups
	}
	return
}
```

The Core Controller will check it it should handle the ClusterQueue or not. If yes - it will perform the **Default Behavior**.

```go
  // pkg/controller/core/clusterqueue_controller.go

  if isMK, err := admissioncheck.QuotaManagedByMultiKueue(ctx, r.client, &cqObj); err != nil {
		return ctrl.Result{}, err
	} else if isMK {
		log.V(2).Info("Skipping EffectiveResourceGroup sync: MultiKueue Manager ClusterQueue quota is managed by a dedicated MultiKueue controller.")
	} else if queue.SyncEffectiveResourceGroupToSpec(&cqObj) {
		log.V(2).Info("Syncing EffectiveResourceGroups to spec.")
		if err := r.client.Status().Update(ctx, &cqObj); err != nil {
			return ctrl.Result{}, err
		}
	}
```

```go
// pkg/util/admissioncheck/admissioncheck.go

func QuotaManagedByMultiKueue(ctx context.Context, c client.Client, cq *kueue.ClusterQueue) (isMK bool, err error) {
	isMK = false
	err = nil
	if features.Enabled(features.MultiKueue) && features.Enabled(features.MultiKueueManagerQuotaAutomation) {
		_, isMK, err = GetMultiKueueAdmissionCheck(ctx, c, cq)
	}
	return
}
```

The MultiKueue Controller is created only when both the features.MultiKueue and features.MultiKueueManagerQuotaAutomation are enabled. The Controller will:
1. Check if the ClusterQueue has the MultiKueue admission check assigned. If not: we will skip adjusting the effective quota as it will be handled by the Core Controller.
1. Check if:
    1. the ClusterQueue's MultiKueue Config has the **Quota Management Strategy** set to "Automated",
    1. spec.ResourceGroups is empty.
1. If any of the above conditions are false: it will perform the **Default Behavior**.
1. If both conditions are true: the controller will calculate the aggregated quota accross the Manager Queue's Workers and set it as the value of status.EffectiveResourceGroups.

```go
func (r *cqReconciler) Reconcile(ctx context.Context, req reconcile.Request) (reconcile.Result, error) {
	log := ctrl.LoggerFrom(ctx).WithValues("clusterQueue", req.Name)
	log.V(3).Info("Reconcile ClusterQueue event received (in the MultiKueue controller)")

	cq := &kueue.ClusterQueue{}
	if err := r.client.Get(ctx, req.NamespacedName, cq); err != nil {
		return reconcile.Result{}, client.IgnoreNotFound(err)
	}

	ac, hasAC, err := admissioncheck.GetMultiKueueAdmissionCheck(ctx, r.client, cq)
	if err != nil {
		return reconcile.Result{}, err
	}
	if !hasAC {
    // Skip - not an MK ClusterQueue
		log.V(3).Info("Not a MultiKueue manager ClusterQueue, skipping reconcile.")
		err := r.removeQuotaAutomationCondition(ctx, cq)
		return reconcile.Result{}, err
	}

	log.V(2).Info("Reconciling MultiKueue manager ClusterQueue")

	cfg, err := r.helper.ConfigFromRef(ctx, ac.Spec.Parameters)
	if err != nil {
    // Illegal state: error
		if apierrors.IsNotFound(err) {
			err = r.updateQuotaAutomationCondition(ctx, cq, metav1.ConditionFalse, kueue.UnsupportedQuotaAutomationConfiguration, "The referenced MultiKueueConfig was not found.")
		}
		return reconcile.Result{}, err
	}

	if ptr.Deref(cfg.Spec.QuotaManagement, kueue.QuotaManagementManual) == kueue.QuotaManagementManual {
    // Legal state: Default Behavior
		err = r.syncEffectiveQuotaToSpec(ctx, cq, kueue.QuotaAutoimationNotRequested, "MultiKueue manager quota automation has not been requested.")
		return reconcile.Result{}, err
	}

	if queue.HasResourceGroupSpec(cq) {
    // Legal state: Default Behavior
		return reconcile.Result{}, r.syncEffectiveQuotaToSpec(
			ctx, 
			cq, 
			kueue.UnsupportedQuotaAutomationConfiguration,
			"ResourceGroups set manually in ClusterQueue spec.",
		)
	}

  // Gather effective quota
	aggregatedRGs, err := r.aggregateWorkerQuotas(ctx, cq, cfg)
	if err != nil {
		return reconcile.Result{}, err
	}

	if !equality.Semantic.DeepEqual(cq.Status.EffectiveResourceGroups, *aggregatedRGs) {
    // Update effective quota
		queue.SetEffectiveResourceGroup(cq, aggregatedRGs)
		if err := r.client.Update(ctx, cq); err != nil {
			return reconcile.Result{}, fmt.Errorf("updating ClusterQueue nominal quotas: %w", err)
		}
	}

	return reconcile.Result{}, r.updateQuotaAutomationCondition(ctx, cq, metav1.ConditionTrue, kueue.QuotaAutomated, "ClusterQueue quota is automatically managed based on MultiKueue workers.")
}

func (r *cqReconciler) syncEffectiveQuotaToSpec(ctx context.Context, cq *kueue.ClusterQueue, reason, message string) error {
	needsUpdate := queue.SyncEffectiveResourceGroupToSpec(cq)

	oldCondition := apimeta.FindStatusCondition(cq.Status.Conditions, kueue.MultiKueueManagerQuotaAutomation)
	newCondition := metav1.Condition{
		Type:               kueue.MultiKueueManagerQuotaAutomation,
		Status:             metav1.ConditionFalse,
		Reason:             reason,
		Message:            message,
		ObservedGeneration: cq.Generation,
	}
	if !cmpConditionState(oldCondition, &newCondition) {
		needsUpdate = true
	  apimeta.SetStatusCondition(&cq.Status.Conditions, newCondition)
	}

	if needsUpdate {
		return r.client.Status().Update(ctx, cq)
	}
	return nil
}
```

### Test Plan

<!--
**Note:** *Not required until targeted at a release.*
The goal is to ensure that we don't accept enhancements with inadequate testing.

All code is expected to have adequate tests (eventually with coverage
expectations). Please adhere to the [Kubernetes testing guidelines][testing-guidelines]
when drafting this test plan.

[testing-guidelines]: https://git.k8s.io/community/contributors/devel/sig-testing/testing.md
-->

[ ] I/we understand the owners of the involved components may require updates to
existing tests to make this code solid enough prior to committing the changes necessary
to implement this enhancement.

#### Prerequisite testing updates

<!--
Based on reviewers feedback describe what additional tests need to be added prior
implementing this enhancement to ensure the enhancements have also solid foundations.
-->

#### Unit tests

<!--
In principle every added code should have complete unit test coverage, so providing
the exact set of tests will not bring additional value.
However, if complete unit test coverage is not possible, explain the reason of it
together with explanation why this is acceptable.
-->

<!--
Additionally, try to enumerate the core package you will be touching
to implement this enhancement and provide the current unit coverage for those
in the form of:
- <package>: <date> - <current test coverage>

This can inform certain test coverage improvements that we want to do before
extending the production code to implement this enhancement.
-->

- `<package>`: `<date>` - `<test coverage>`

#### Integration tests

<!--
Describe what tests will be added to ensure proper quality of the enhancement.

After the implementation PR is merged, add the names of the tests here.
-->

#### e2e tests

<!--
This question should be filled when targeting a release.
For Alpha, describe what tests will be added to ensure proper quality of the enhancement.

For Beta and GA, document that tests have been written,
have been executed regularly, and have been stable.
This can be done with:
- permalinks to the GitHub source code
- links to the periodic job (typically a job owned by the SIG responsible for the feature), filtered by the test name

If e2e tests are not necessary or useful, explain why.
-->

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

## Implementation History

<!--
Major milestones in the lifecycle of a KEP should be tracked in this section.
Major milestones might include:
- the `Summary` and `Motivation` sections being merged, signaling SIG acceptance
- the `Proposal` section being merged, signaling agreement on a proposed design
- the date implementation started
- the first Kubernetes release where an initial version of the KEP was available
- the version of Kubernetes where the KEP graduated to general availability
- when the KEP was retired or superseded
-->

## Drawbacks

<!--
Why should this KEP _not_ be implemented?
-->

## Alternatives

<!--
What other approaches did you consider, and why did you rule them out? These do
not need to be as detailed as the proposal, but should include enough
information to express the idea and why it was not acceptable.
-->
