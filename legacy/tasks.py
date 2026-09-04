from dotenv import load_dotenv
load_dotenv()

from director_agent import DirectorAgent

director = DirectorAgent()
director.handle_task("Find a product on ClickBank and run ads on Facebook.")

from adcopy_agent import AdCopyAgent
from scraper_agent import ScraperAgent
from optimizer_agent import OptimizerAgent
from review_agent import ReviewAgent

# Optional: If you want to use Facebook calls, import FbAgent
# from fb_agent import FbAgent

class TaskManager:
    """
    Coordinates a basic sequence:
    1. Generate ad copy
    2. Review it
    3. Scrape competitor ads
    4. Optimize
    (Optionally create or update ads on Facebook)
    """

    def __init__(self):
        self.ad_agent = AdCopyAgent()
        self.scraper_agent = ScraperAgent()
        self.optimizer_agent = OptimizerAgent()
        self.review_agent = ReviewAgent()

        # Uncomment if you want to integrate Facebook tasks:
        # self.fb_agent = FbAgent()

    def run_campaign(self, product_name, url, keyword, ad_performance=None):
        # 1. Generate ad copy
        product = {"product_name": product_name}
        ad_copy = self.ad_agent.run(product)
        print(f"Ad Copy Generated:\n{ad_copy}\n")

        # 2. Review the ad copy
        print("🔍 Reviewing Ad Copy...")
        review = self.review_agent.review(ad_copy, ad_performance)
        print(review, "\n")

        # 3. Scrape competitor ads
        print("Scraping competitor ads...")
        competitor_ads = self.scraper_agent.scrape_competitor_ads(keyword)
        print(competitor_ads, "\n")

        # 4. Optimize ad performance
        print("Optimizing ad performance...")
        optimization_result = self.optimizer_agent.optimize(ad_copy)
        print(optimization_result, "\n")

        # 5. (Optional) Create a Facebook campaign using the ad copy
        # Example:
        # new_campaign = self.fb_agent.create_campaign(name=f"{product_name} Campaign")
        # print("Created campaign:", new_campaign)

        return {
            "ad_copy": ad_copy,
            "review_feedback": review,
            "competitor_ads": competitor_ads,
            "optimization_result": optimization_result
        }


if __name__ == "__main__":
    manager = TaskManager()
    manager.run_campaign(
        product_name="Smartwatch", 
        url="https://www.example.com", 
        keyword="Smartwatch Ads", 
        ad_performance={"ctr": 1.2}
    )
