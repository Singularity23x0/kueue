package scheduler

import (
	"context"

	"k8s.io/klog/v2"
	"sigs.k8s.io/controller-runtime/pkg/log"
	schdcache "sigs.k8s.io/kueue/pkg/cache/scheduler"
	"sigs.k8s.io/kueue/pkg/features"
	"sigs.k8s.io/kueue/pkg/scheduler/flavorassigner"
	"sigs.k8s.io/kueue/pkg/scheduler/preemption"
	"sigs.k8s.io/kueue/pkg/workload"
)

type schedulerSimulator interface {
	Schedule(ctx context.Context, cq *schdcache.ClusterQueueSnapshot, wl *workload.Info, preemptionTargets []*preemption.Target, counts []int32) (*flavorassigner.Assignment, []*preemption.Target, bool)
	Finalize(
		ctx context.Context,
		assignment *flavorassigner.Assignment,
		cq *schdcache.ClusterQueueSnapshot,
		wl *workload.Info,
		targets []*preemption.Target,
	)
}

type internalSimulator struct {
	snap        *schdcache.Snapshot
	flvAssigner *flavorassigner.FlavorAssigner
	preemptor   *preemption.Preemptor
}

func (s *internalSimulator) Schedule(ctx context.Context, cq *schdcache.ClusterQueueSnapshot, wl *workload.Info, preemptionTargets []*preemption.Target, counts []int32) (*flavorassigner.Assignment, []*preemption.Target, bool) {
	log := log.FromContext(ctx)
	assignment, failed := s.flvAssigner.AssignFlavors(ctx, log, counts)
	if !failed {
		s.flvAssigner.AssignTopology(ctx, log, &assignment)
		assignment.ResolveNoFitReason(cq)
	}
	arm := assignment.RepresentativeMode()
	if arm == flavorassigner.Fit {
		return &assignment, preemptionTargets, true
	}
	if arm == flavorassigner.Preempt {
		faPreemptionTargets := s.preemptor.GetTargets(ctx, *wl, assignment, s.snap)
		if len(faPreemptionTargets) > 0 {
			return &assignment, append(preemptionTargets, faPreemptionTargets...), true
		}
	}
	return &assignment, preemptionTargets, false
}

func (s *internalSimulator) Finalize(
	ctx context.Context,
	assignment *flavorassigner.Assignment,
	cq *schdcache.ClusterQueueSnapshot,
	wl *workload.Info,
	targets []*preemption.Target,
) {
	log := log.FromContext(ctx)
	if features.Enabled(features.TopologyAwareScheduling) && assignment.RepresentativeMode() == flavorassigner.Preempt &&
		(workload.IsExplicitlyRequestingTAS(wl.Obj.Spec.PodSets...) || cq.IsTASOnly()) && !workload.HasTopologyAssignmentWithUnhealthyNode(wl.Obj) {
		tasRequests := assignment.WorkloadsTopologyRequests(log, wl, cq)
		var tasResult schdcache.TASAssignmentsResult
		log = log.WithValues("workload", klog.KRef(wl.Obj.Namespace, wl.Obj.Name))

		if len(targets) > 0 {
			var targetWorkloads []*workload.Info
			for _, target := range targets {
				targetWorkloads = append(targetWorkloads, target.WorkloadInfo)
			}
			revertUsage := s.snap.SimulateWorkloadUsageRemoval(targetWorkloads)
			// Freeing the victims' quota is not enough. Until the simulator is told,
			// it still reports their Pods and their nodes still look occupied.
			revertPods := simulatePodRemoval(ctx, log, s.snap, targetWorkloads)
			tasResult = cq.FindTopologyAssignmentsForWorkload(
				ctx,
				tasRequests,
				schdcache.WithWorkload(wl.Obj),
			)
			revertPods()
			revertUsage()
		} else {
			// In this scenario we don't have any preemption candidates, yet we need
			// to reserve the TAS resources to avoid the situation when a lower
			// priority workload further in the queue gets admitted and preempted
			// in the next scheduling cycle by the waiting workload. To obtain
			// a TAS assignment for reserving the resources we run the algorithm
			// assuming the cluster is empty.
			tasResult = cq.FindTopologyAssignmentsForWorkload(
				ctx,
				tasRequests,
				schdcache.WithSimulateEmpty(true),
				schdcache.WithWorkload(wl.Obj),
			)
		}
		assignment.UpdateForTASResult(log, cq, wl, tasResult)
	}
}

// type schedLibSimulator struct {
// 	flvAssigner *flavorassigner.FlavorAssigner
// 	simSnap     simulator.SimulatorSnapshot
// }

// func (s *schedLibSimulator) Schedule(ctx context.Context, wl *workload.Info, snap *schdcache.Snapshot, preemptionTargets []*preemption.Target, cq *schdcache.ClusterQueueSnapshot, counts []int32) (*flavorassigner.Assignment, []*preemption.Target, bool) {
// 	log := log.FromContext(ctx)
// 	assignment, _ := s.flvAssigner.AssignFlavors(ctx, log, counts)

// 	var preemptionCandidates []client.ObjectKey
// 	var alreadyPreempted []client.ObjectKey
// 	schedulingResult, preemptionsResult, err := s.simSnap.ScheduleWorkload(ctx, client.ObjectKeyFromObject(wl.Obj), preemptionCandidates, alreadyPreempted)

// 	if err != nil {
// 		// we fail
// 	}

// 	// integrate schedulingResult into assignment
// 	// add preemptionsResult to preemptionTargets

// 	return &assignment, preemptionTargets, false
// }

// func (s *schedLibSimulator) Finalize(
// 	ctx context.Context,
// 	snapshot *schdcache.Snapshot,
// 	cq *schdcache.ClusterQueueSnapshot,
// 	wl *workload.Info,
// 	assignment *flavorassigner.Assignment,
// 	targets []*preemption.Target,
// ) {
// 	panic("not implemented")
// }
