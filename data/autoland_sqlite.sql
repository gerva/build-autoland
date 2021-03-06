--
-- Table structure for table `branches`
--

DROP TABLE IF EXISTS `branches`;
CREATE TABLE `branches` (
  `id` INTEGER PRIMARY KEY,
  `name` text,
  `repo_url` text,
  `threshold` int(11) DEFAULT NULL,
  `status` text,
  `push_to_closed` INTEGER,
  `approval_required` INTEGER,
  UNIQUE(`name`)
);
--
-- Table structure for table `patch_sets`
--

DROP TABLE IF EXISTS `patch_sets`;
CREATE TABLE `patch_sets` (
  `id` INTEGER PRIMARY KEY,
  `bug_id` int(11) DEFAULT NULL,
  `patches` text,
  `author` text,
  `retries` int(11) DEFAULT NULL,
  `revision` text,
  `branch` text,
  `try_run` int(11) DEFAULT NULL,
  `try_syntax` text,
  `creation_time` timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `push_time` timestamp NULL DEFAULT NULL,
  `completion_time` timestamp NULL DEFAULT NULL
);
--
-- Table structure for table `complete`
--

DROP TABLE IF EXISTS `complete`;
CREATE TABLE `complete` (
  `id` INTEGER PRIMARY KEY,
  `bug_id` int(11) DEFAULT NULL,
  `patches` text,
  `author` text,
  `retries` int(11) DEFAULT NULL,
  `revision` text,
  `branch` text,
  `try_run` int(11) DEFAULT NULL,
  `try_syntax` text,
  `creation_time` timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `push_time` timestamp NULL DEFAULT NULL,
  `completion_time` timestamp NULL DEFAULT NULL,
  `status` text DEFAULT NULL
);

--
-- Table structure for table `comments`
--

DROP TABLE IF EXISTS `comments`;
CREATE TABLE `comments` (
    `id` INTEGER PRIMARY KEY,
    `comment` text,
    `bug` int(11) DEFAULT NULL,
    `attempts` int(11),
    `insertion_time` timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP
);
