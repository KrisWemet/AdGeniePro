import requests
from bs4 import BeautifulSoup

class ScraperAgent:
    """
    Scrapes competitor ads from the Facebook Ads Library 
    or other public sources (basic example).
    """

    def scrape_competitor_ads(self, keyword):
        # This URL might not show the ads properly if Facebook blocks direct scrapes
        # In reality, you may need a different approach or official Ads Library API
        url = f"https://www.facebook.com/ads/library/?q={keyword}&ad_type=all"
        response = requests.get(url)
        soup = BeautifulSoup(response.text, "html.parser")

        # Extract ad titles or relevant text
        titles = [tag.text for tag in soup.find_all("h4")]
        
        if titles:
            return f"Top Competitor Ads for '{keyword}':\n" + "\n".join(titles[:5])
        else:
            return f"No ads found for '{keyword}'. Try a different search term."


if __name__ == "__main__":
    agent = ScraperAgent()
    print(agent.scrape_competitor_ads("Smartwatch"))
