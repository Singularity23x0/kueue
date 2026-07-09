---
name: New Release
about: Propose a new release
title: Release v0.x.0
assignees: mimowo, tenzen-y

---

## Release Checklist
<!--
Please do not remove items from the checklist
-->
- [ ] [OWNERS](https://github.com/kubernetes-sigs/kueue/blob/main/OWNERS) must LGTM the release proposal.
  At least two for minor or major releases. At least one for a patch release.
- [ ] Verify that the changelog in this issue and the CHANGELOG folder is up-to-date
  - [ ] Run ChatOps command `/sync-release-notes` to generate and publish the release notes
- [ ] For major or minor releases (v$MAJ.$MIN.0), create a new release branch.
  - [ ] An OWNER creates a vanilla release branch with
        `git branch release-$MAJ.$MIN main`
  - [ ] An OWNER pushes the new release branch with
        `git push upstream release-$MAJ.$MIN`
- [ ] Update the release branch:
  - [ ] Run ChatOps command `/prepare-release-branch` on this issue to generate version updates and open a PR.
  - [ ] Wait for this PR to merge <!-- PREPARE_PULL_RELEASE --> <!-- example #211 -->
- [ ] Run ChatOps command `/release` on this issue. This will:
  - Create and sign the release tag.
  - Push the tag (triggers Prow to build and publish staging container image: `us-central1-docker.pkg.dev/k8s-staging-images/kueue/kueue:$VERSION`).
  - Compile release artifacts and create a draft release.
  - For major/minor releases, tag the next devel version and create the GitHub milestone automatically.
- [ ] Promote images and Helm Charts to production:
  - [ ] Run `./hack/releasing/wait_for_images.sh $VERSION` to await the staging images.
  - [ ] Run `./hack/releasing/promote_pull.sh $VERSION` to submit the promotion PR.
  - [ ] Wait for the PR to be merged <!-- K8S_IO_PULL --> <!-- example kubernetes/k8s.io#7899 -->
  - [ ] Run: `./hack/releasing/wait_for_images.sh --prod $VERSION` to verify that the promoted images are available.
- [ ] Run ChatOps command `/publish-release` on this issue to publish the draft release.
  - This automatically triggers the SBOM and OpenVEX generation webhooks which upload metadata to the published release.
- [ ] Update the `main` branch:
  - [ ] Run ChatOps command `/prepare-main-branch` on this issue.
  - [ ] Wait for this PR to merge <!-- PREPARE_PULL_MAIN --> <!-- example #214 -->
  - [ ] Cherry-pick the pull request onto the `website` branch.
- [ ] For major or minor releases, merge the `main` branch into the `website` branch to publish the updated documentation.
- [ ] Send an announcement email to `sig-scheduling@kubernetes.io` and `wg-batch@kubernetes.io` with the subject `[ANNOUNCE] kueue $VERSION is released`.   <!--Link: example https://groups.google.com/a/kubernetes.io/g/wg-batch/c/-gZOrSnwDV4 -->
- [ ] For a major or minor release, prepare the repo for the next version:
  - [ ] Prow Job Updates: Create the presubmits and the periodic jobs for the next patch release, and drop jobs for the out-of-support branch: <!-- CI_PULL -->
        <!-- example: https://github.com/kubernetes/test-infra/pull/34561 -->


## Changelog

```markdown
Describe changes since the last release here.
```
